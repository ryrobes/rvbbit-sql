"""Focused contracts for incremental Workspace auth and Google Sheets export."""
from __future__ import annotations

import asyncio
import inspect
import json
import sys
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
import auth  # noqa: E402
import calliope  # noqa: E402
import server  # noqa: E402


class _Result:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


def test_workspace_schema_oauth_tool_and_native_surface_ship_together():
    migration = (
        ROOT / "crates" / "pg_rvbbit" / "sql" / "migrations"
        / "0245_calliope_google_workspace.sql"
    ).read_text(encoding="utf-8")
    registry = (ROOT / "crates" / "pg_rvbbit" / "src" / "migrations.rs").read_text(
        encoding="utf-8"
    )
    backend = (HERE / "calliope.py").read_text(encoding="utf-8")
    auth_source = (HERE / "auth.py").read_text(encoding="utf-8")
    server_source = (HERE / "server.py").read_text(encoding="utf-8")
    script = (HERE / "calliope" / "calliope.js").read_text(encoding="utf-8")

    assert "0245_calliope_google_workspace" in registry
    assert "owner_email text PRIMARY KEY" in migration
    assert "refresh_token_ciphertext text NOT NULL" in migration
    assert "calliope_google_exports" in migration
    assert "conn.execute(_GOOGLE_WORKSPACE_DDL)" in backend
    assert '@mcp.custom_route("/api/calliope/workspace", methods=["GET"])' in backend
    assert "google-sheet" in backend
    assert '@mcp.custom_route("/auth/google/workspace/start", methods=["GET"])' in auth_source
    assert "https://www.googleapis.com/auth/drive.file" in auth_source
    assert 'mcp.tool(name="export_to_google_sheets")' in server_source
    assert "data-export-google-sheet" in script
    assert "calliope.pendingWorkspaceExport.v1" in script


def test_workspace_tokens_are_purpose_separated_and_cells_are_bounded(monkeypatch):
    monkeypatch.setenv("WAREHOUSE_GOOGLE_TOKEN_KEY", "workspace-test-key")
    token = "1//workspace-private-refresh-token"
    ciphertext = calliope._encrypt_google_workspace_token(token)
    calendar_ciphertext = calliope._encrypt_google_calendar_token(token)

    assert token not in ciphertext
    assert calliope._decrypt_google_workspace_token(ciphertext) == token
    assert ciphertext != calendar_ciphertext

    names, rows = calliope._google_sheet_values(
        [{"name": "name"}, {"name": "facts"}, {"name": "score"}],
        [{"name": "Ada", "facts": {"team": "Data"}, "score": float("inf")}],
    )
    assert names == ["name", "facts", "score"]
    assert rows == [["Ada", '{"team": "Data"}', "inf"]]


def test_workspace_oauth_is_incremental_single_use_and_account_bound(monkeypatch):
    routes = {}

    class MCP:
        @staticmethod
        def custom_route(path, methods):
            def register(handler):
                routes[(path, tuple(methods))] = handler
                return handler
            return register

    class Provider:
        public = "https://warehouse.example"

        @staticmethod
        def has_pending(_txn):
            return True

    monkeypatch.setattr(auth, "GOOGLE_CLIENT_ID", "test.apps.googleusercontent.com")
    monkeypatch.setattr(auth, "GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setattr(auth, "GOOGLE_HD", "example.com")
    monkeypatch.setattr(auth, "_GWORK_FLOWS", auth._GoogleWorkspaceFlows())
    monkeypatch.setattr(auth, "_GCAL_FLOWS", auth._GoogleCalendarFlows())
    monkeypatch.setattr(auth, "_GFLOWS", auth._GoogleFlows())
    monkeypatch.setattr(auth, "read_session_full", lambda _request: {
        "identity": "owner@example.com", "mapped": True, "via": "google",
    })
    grants = []

    async def grant(owner, payload):
        grants.append((owner, payload))

    auth.register_login_route(MCP(), Provider(), google_workspace_grant=grant)

    class Request:
        headers = {}
        client = None

        def __init__(self, query):
            self.query_params = query

    start = asyncio.run(routes[("/auth/google/workspace/start", ("GET",))](
        Request({"next": "/calliope?session=abc"})
    ))
    params = parse_qs(urlparse(start.headers["location"]).query)
    assert params["scope"] == [f"openid email {calliope._GOOGLE_WORKSPACE_SCOPE}"]
    assert params["access_type"] == ["offline"]
    assert params["include_granted_scopes"] == ["true"]
    assert params["prompt"] == ["consent"]

    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {
                "id_token": "signed-token",
                "access_token": "access",
                "refresh_token": "refresh",
                "scope": f"openid email {calliope._GOOGLE_WORKSPACE_SCOPE}",
            }

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            return Response()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    monkeypatch.setattr(auth, "google_domain_ok", lambda _claims: True)
    monkeypatch.setattr(auth, "_email_allowed", lambda _email: True)
    verified = {"email": "owner@example.com"}
    monkeypatch.setattr(auth, "verify_google_id_token", lambda *_args: {
        "email": verified["email"], "email_verified": True,
    })
    callback = routes[("/auth/google/callback", ("GET",))]
    connected = asyncio.run(callback(Request({"state": params["state"][0], "code": "code"})))
    assert parse_qs(urlparse(connected.headers["location"]).query)["workspace"] == ["connected"]
    assert grants[0][0] == "owner@example.com"
    replay = asyncio.run(callback(Request({"state": params["state"][0], "code": "code"})))
    assert replay.status_code == 400


def test_google_sheet_export_writes_raw_values_formats_header_and_records_receipt(monkeypatch):
    export_id = None
    sql_calls = []
    http_calls = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            nonlocal export_id
            sql_calls.append((query, params))
            if query.startswith("INSERT INTO rvbbit.calliope_google_exports"):
                export_id = params[0]
                return _Result()
            if "UPDATE rvbbit.calliope_google_exports SET status='complete'" in query:
                return _Result({
                    "id": export_id,
                    "owner_email": "owner@example.com",
                    "session_id": None,
                    "surface_id": None,
                    "provider": "google_sheets",
                    "provider_file_id": params[0],
                    "title": "Quarterly pipeline",
                    "url": params[1],
                    "sheet_name": "Pipeline",
                    "row_count": 2,
                    "column_count": 2,
                    "status": "complete",
                    "source": {"origin": "test"},
                    "created_at": "2026-08-05T12:00:00Z",
                    "completed_at": "2026-08-05T12:00:01Z",
                })
            return _Result()

    class Response:
        def __init__(self, payload):
            self.status_code = 200
            self._payload = payload

        def json(self):
            return self._payload

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, **kwargs):
            http_calls.append(("POST", url, kwargs))
            if url == calliope._GOOGLE_SHEETS_CREATE_URL:
                return Response({
                    "spreadsheetId": "sheet-123",
                    "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/sheet-123/edit",
                    "sheets": [{"properties": {"sheetId": 7, "title": "Pipeline"}}],
                })
            return Response({})

        async def put(self, url, **kwargs):
            http_calls.append(("PUT", url, kwargs))
            return Response({"updatedRows": 3})

    monkeypatch.setattr(calliope, "_google_workspace_connection", lambda *_args: {
        "status": "connected",
    })

    async def token(*_args):
        return "short-lived-access-token"

    monkeypatch.setattr(calliope, "_google_workspace_access_token", token)
    monkeypatch.setattr(calliope.httpx, "AsyncClient", Client)

    receipt = asyncio.run(calliope.export_google_sheet(
        Connection,
        "owner@example.com",
        "Quarterly pipeline",
        [{"name": "region"}, {"name": "revenue"}],
        [{"region": "East", "revenue": 12}, {"region": "West", "revenue": 9}],
        sheet_name="Pipeline",
        source={"origin": "test"},
    ))

    assert receipt["provider_file_id"] == "sheet-123"
    assert receipt["status"] == "complete"
    write = next(call for call in http_calls if call[0] == "PUT")
    assert write[2]["params"] == {"valueInputOption": "RAW"}
    assert write[2]["json"]["values"] == [
        ["region", "revenue"], ["East", 12], ["West", 9],
    ]
    formatting = next(call for call in http_calls if call[1].endswith(":batchUpdate"))
    assert formatting[2]["json"]["requests"][0]["updateSheetProperties"]["properties"]["gridProperties"] == {
        "frozenRowCount": 1,
    }
    assert any("status='complete'" in query for query, _params in sql_calls)
    assert all("short-lived-access-token" not in json.dumps(params, default=str) for _query, params in sql_calls)


def test_mcp_export_resolves_owner_runs_governed_sql_and_returns_receipt(monkeypatch):
    monkeypatch.setattr(server, "_logged", lambda _name, _args, function: function())
    monkeypatch.setattr(calliope, "is_enabled", lambda: True)
    monkeypatch.setattr(
        calliope,
        "_owner_for_calliope_session",
        lambda _factory, _session: "owner@example.com",
    )
    monkeypatch.setattr(server, "tool_run_sql", lambda *_args: {
        "columns": [{"name": "value"}],
        "rows": [{"value": 42}],
        "row_count": 1,
        "truncated": False,
        "engine": "postgres",
        "as_of_applied": None,
    })

    async def export(_factory, owner, title, columns, rows, **kwargs):
        assert owner == "owner@example.com"
        assert title == "Answer"
        assert columns == [{"name": "value"}]
        assert rows == [{"value": 42}]
        assert kwargs["session_id"] == "11111111-1111-4111-8111-111111111111"
        return {
            "id": str(uuid.uuid4()),
            "provider": "google_sheets",
            "title": title,
            "url": "https://docs.google.com/spreadsheets/d/example/edit",
            "row_count": 1,
            "column_count": 1,
            "status": "complete",
        }

    monkeypatch.setattr(calliope, "export_google_sheet", export)
    monkeypatch.setattr(auth, "AUTH_MODE", "shared")

    result = server._mcp_export_to_google_sheets(
        "11111111-1111-4111-8111-111111111111",
        "select 42 as value",
        "Answer",
    )

    assert result["sheet"]["status"] == "complete"
    assert result["query"] == {
        "row_count": 1,
        "column_count": 1,
        "truncated": False,
        "engine": "postgres",
        "as_of_applied": None,
    }


def test_native_export_reads_only_the_owners_frozen_query_surface(monkeypatch):
    session_id = str(uuid.uuid4())
    surface_id = str(uuid.uuid4())
    turn_id = str(uuid.uuid4())
    observed = {}
    surface_row = {
        "id": surface_id,
        "session_id": session_id,
        "turn_id": turn_id,
        "ordinal": 1,
        "kind": "query",
        "title": "Current pipeline",
        "tool_name": "run_sql",
        "tool_call_id": "call-1",
        "lineage_key": "query:test",
        "parent_surface_id": None,
        "artifact_slug": None,
        "artifact_version": None,
        "payload": {
            "columns": [{"name": "stage"}, {"name": "count"}],
            "rows": [{"stage": "Open", "count": 7}],
        },
        "source": {"sql": "select stage, count(*) from pipeline group by stage"},
        "presentation": {},
        "created_at": "2026-08-05T12:00:00Z",
    }

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            if query.startswith("SELECT f.* FROM rvbbit.calliope_surfaces"):
                observed["select"] = (query, params)
                return _Result(surface_row)
            if query.startswith("UPDATE rvbbit.calliope_surfaces SET presentation"):
                updated = {**surface_row, "presentation": {
                    "google_sheet": json.loads(params[0]),
                }}
                return _Result(updated)
            raise AssertionError(query)

    async def export(_factory, owner, title, columns, rows, **kwargs):
        observed["export"] = {
            "owner": owner,
            "title": title,
            "columns": columns,
            "rows": rows,
            **kwargs,
        }
        return {
            "id": str(uuid.uuid4()),
            "provider": "google_sheets",
            "provider_file_id": "sheet-123",
            "title": title,
            "url": "https://docs.google.com/spreadsheets/d/sheet-123/edit",
            "sheet_name": "Data",
            "row_count": 1,
            "column_count": 2,
            "status": "complete",
            "completed_at": "2026-08-05T12:00:01Z",
        }

    monkeypatch.setattr(calliope, "export_google_sheet", export)
    result = asyncio.run(calliope.export_query_surface_to_google_sheet(
        Connection,
        "owner@example.com",
        session_id,
        surface_id,
    ))

    assert observed["select"][1] == (surface_id, session_id, "owner@example.com")
    assert "lower(s.owner_email)=lower(%s)" in observed["select"][0]
    assert observed["export"]["rows"] == [{"stage": "Open", "count": 7}]
    assert observed["export"]["surface_id"] == surface_id
    assert result["surface"]["presentation"]["google_sheet"]["url"].startswith(
        "https://docs.google.com/"
    )


def test_sheet_receipt_projects_as_openable_stage_document():
    export_id = str(uuid.uuid4())
    projected = calliope._project_tool_result(
        "export_to_google_sheets",
        {"sheet": {
            "id": export_id,
            "provider": "google_sheets",
            "title": "Pipeline",
            "url": "https://docs.google.com/spreadsheets/d/example/edit",
            "row_count": 12,
            "column_count": 4,
        }},
        {"session_id": str(uuid.uuid4()), "title": "Pipeline"},
        "tool-call-1",
    )

    assert projected[0]["kind"] == "document"
    assert projected[0]["payload"]["provider"] == "google_sheets"
    assert projected[0]["payload"]["download_url"].startswith("https://docs.google.com/")
    assert "export_to_google_sheets" in inspect.getsource(calliope._project_tool_result)
