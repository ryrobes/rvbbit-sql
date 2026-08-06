"""Focused contracts for incremental Workspace auth and Google Sheets IO."""
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
    import_migration = (
        ROOT / "crates" / "pg_rvbbit" / "sql" / "migrations"
        / "0246_calliope_google_sheet_imports.sql"
    ).read_text(encoding="utf-8")
    document_migration = (
        ROOT / "crates" / "pg_rvbbit" / "sql" / "migrations"
        / "0247_calliope_google_document_imports.sql"
    ).read_text(encoding="utf-8")
    document_lifecycle_migration = (
        ROOT / "crates" / "pg_rvbbit" / "sql" / "migrations"
        / "0248_calliope_google_document_lifecycle.sql"
    ).read_text(encoding="utf-8")
    sheet_lifecycle_migration = (
        ROOT / "crates" / "pg_rvbbit" / "sql" / "migrations"
        / "0249_calliope_google_sheet_lifecycle.sql"
    ).read_text(encoding="utf-8")
    registry = (ROOT / "crates" / "pg_rvbbit" / "src" / "migrations.rs").read_text(
        encoding="utf-8"
    )
    backend = (HERE / "calliope.py").read_text(encoding="utf-8")
    auth_source = (HERE / "auth.py").read_text(encoding="utf-8")
    server_source = (HERE / "server.py").read_text(encoding="utf-8")
    script = (HERE / "calliope" / "calliope.js").read_text(encoding="utf-8")
    page = (HERE / "calliope" / "index.html").read_text(encoding="utf-8")
    compose = (ROOT / "docker" / "docker-compose.uber.yml").read_text(encoding="utf-8")

    assert "0245_calliope_google_workspace" in registry
    assert "0246_calliope_google_sheet_imports" in registry
    assert "0247_calliope_google_document_imports" in registry
    assert "0248_calliope_google_document_lifecycle" in registry
    assert "0249_calliope_google_sheet_lifecycle" in registry
    assert "owner_email text PRIMARY KEY" in migration
    assert "refresh_token_ciphertext text NOT NULL" in migration
    assert "calliope_google_exports" in migration
    assert "calliope_google_imports" in import_migration
    assert "snapshot_hash text NOT NULL" in import_migration
    assert "calliope_google_document_imports" in document_migration
    assert "brain_doc_id bigint NOT NULL" in document_migration
    assert "private_role text NOT NULL" in document_migration
    assert "status IN ('active','superseded','removed')" in document_lifecycle_migration
    assert "calliope_google_document_imports_active_file_uidx" in document_lifecycle_migration
    assert "status IN ('active','superseded')" in sheet_lifecycle_migration
    assert "calliope_google_imports_active_source_uidx" in sheet_lifecycle_migration
    assert "conn.execute(_GOOGLE_WORKSPACE_DDL)" in backend
    assert backend.index("ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'active'") < backend.index(
        "CREATE UNIQUE INDEX IF NOT EXISTS calliope_google_document_imports_active_file_uidx"
    )
    assert '@mcp.custom_route("/api/calliope/workspace", methods=["GET"])' in backend
    assert "google-sheet" in backend
    assert '@mcp.custom_route("/auth/google/workspace/start", methods=["GET"])' in auth_source
    assert "https://www.googleapis.com/auth/drive.file" in auth_source
    assert 'mcp.tool(name="export_to_google_sheets")' in server_source
    assert "data-export-google-sheet" in script
    assert 'class="surface-sheet-link"' in script
    assert 'class="google-sheets-glyph"' in script
    assert "calliope.pendingWorkspaceExport.v1" in script
    assert "calliope.pendingWorkspacePicker.v1" in script
    assert "https://apis.google.com/js/api.js" in script
    assert "workspace/google-sheet/inspect" in script
    assert "workspace/google-document/inspect" in script
    assert "google-document-import" in script
    assert "data-refresh-google-document" in script
    assert "data-forget-google-document" in script
    assert "data-refresh-google-sheet" in script
    assert "google-document/{surface_id}/refresh" in backend
    assert "google-sheet/{surface_id}/refresh" in backend
    assert 'id="google-sheet-import"' in page
    assert 'id="sheet-import-dialog"' in page
    assert 'id="google-document-import"' in page
    assert 'id="document-import-dialog"' in page
    assert "WAREHOUSE_GOOGLE_PICKER_API_KEY" in compose


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


def test_picker_settings_are_public_but_require_key_and_project_number(monkeypatch):
    monkeypatch.delenv("WAREHOUSE_GOOGLE_PICKER_API_KEY", raising=False)
    monkeypatch.delenv("WAREHOUSE_GOOGLE_PICKER_APP_ID", raising=False)
    assert calliope._google_picker_settings("123456-client.apps.googleusercontent.com") == {
        "enabled": False,
        "api_key": None,
        "app_id": None,
        "mime_type": calliope._GOOGLE_SHEETS_MIME_TYPE,
        "sheet_mime_type": calliope._GOOGLE_SHEETS_MIME_TYPE,
        "document_mime_type": calliope._GOOGLE_DOCS_MIME_TYPE,
    }

    monkeypatch.setenv("WAREHOUSE_GOOGLE_PICKER_API_KEY", "browser-key")
    settings = calliope._google_picker_settings(
        "123456-client.apps.googleusercontent.com"
    )
    assert settings == {
        "enabled": True,
        "api_key": "browser-key",
        "app_id": "123456",
        "mime_type": "application/vnd.google-apps.spreadsheet",
        "sheet_mime_type": "application/vnd.google-apps.spreadsheet",
        "document_mime_type": "application/vnd.google-apps.document",
    }

    monkeypatch.setenv("WAREHOUSE_GOOGLE_PICKER_APP_ID", "987654")
    assert calliope._google_picker_settings("not-numeric.apps.googleusercontent.com")[
        "app_id"
    ] == "987654"


def test_sheet_import_range_and_table_are_bounded_and_stable():
    assert calliope._google_sheet_range_bounds("b2:az20") == {
        "start_column": 2,
        "start_row": 2,
        "end_column": 52,
        "end_row": 20,
        "column_count": 51,
        "row_count": 19,
        "a1": "B2:AZ20",
    }
    api_range, selected_range, clipped = calliope._google_sheet_read_window(
        {"title": "Team's plan", "row_count": 500, "column_count": 40},
        "A1:AN500",
        preview=True,
        first_row_header=True,
    )
    assert api_range == "'Team''s plan'!A1:X31"
    assert selected_range == "A1:AN500"
    assert clipped is True
    full_range, _, full_clipped = calliope._google_sheet_read_window(
        {"title": "Large", "row_count": 50_000, "column_count": 256},
        None,
        preview=False,
        first_row_header=True,
    )
    assert full_range == "'Large'!A1:IV195"
    assert full_clipped is True

    table = calliope._google_sheet_table(
        [
            ["Name", "Score", "Name", "Enabled"],
            ["Ada", 4, "A", True],
            ["Lin", 7.5, "L", False],
        ],
        first_row_header=True,
        max_rows=30,
        max_bytes=100_000,
    )
    assert [column["name"] for column in table["columns"]] == [
        "Name", "Score", "Name_2", "Enabled",
    ]
    assert [column["type"] for column in table["columns"]] == [
        "text", "number", "text", "boolean",
    ]
    assert [column["sql_name"] for column in table["columns"]] == [
        "name", "score", "name_2", "enabled",
    ]
    assert table["rows"][0] == {
        "Name": "Ada", "Score": 4, "Name_2": "A", "Enabled": True,
    }
    byte_bounded = calliope._google_sheet_table(
        [["value"], ["too large"]],
        first_row_header=True,
        max_rows=30,
        max_bytes=1,
    )
    assert byte_bounded["rows"] == []
    assert byte_bounded["truncated"] is True

    try:
        calliope._google_sheet_range_bounds("A:A")
    except ValueError as exc:
        assert "bounded A1 range" in str(exc)
    else:
        raise AssertionError("unbounded ranges must be rejected")


def test_sheet_inspection_reads_only_selected_file_and_never_persists_access_token(monkeypatch):
    sql_calls = []
    http_calls = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            sql_calls.append((query, params))
            return _Result()

    class Response:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, url, **kwargs):
            http_calls.append((url, kwargs))
            if "/values/" in url:
                return Response({
                    "range": "'Pipeline'!A1:D3",
                    "values": [
                        ["Owner", "Amount", "Owner", "Open"],
                        ["Ada", 12, "A", True],
                        ["Lin", 9.5, "L", False],
                    ],
                })
            return Response({
                "spreadsheetId": "sheet_file_12345",
                "properties": {
                    "title": "FY Plan",
                    "locale": "en_US",
                    "timeZone": "America/New_York",
                },
                "sheets": [{"properties": {
                    "sheetId": 7,
                    "title": "Pipeline",
                    "index": 0,
                    "sheetType": "GRID",
                    "gridProperties": {"rowCount": 250, "columnCount": 8},
                }}],
            })

    async def token(*_args):
        return "short-lived-picker-token"

    monkeypatch.setattr(calliope, "_google_workspace_access_token", token)
    monkeypatch.setattr(calliope.httpx, "AsyncClient", Client)

    result = asyncio.run(calliope.inspect_google_sheet(
        Connection,
        "owner@example.com",
        "sheet_file_12345",
        sheet_id=7,
        selected_range="A1:D3",
        first_row_header=True,
        preview=True,
    ))

    assert result["workbook"]["title"] == "FY Plan"
    assert result["sheet"]["title"] == "Pipeline"
    assert result["selected_range"] == "A1:D3"
    assert [column["name"] for column in result["columns"]] == [
        "Owner", "Amount", "Owner_2", "Open",
    ]
    assert result["rows"][0]["Amount"] == 12
    assert len(result["snapshot_hash"]) == 64
    assert len(http_calls) == 2
    assert all(
        call[1]["headers"]["Authorization"] == "Bearer short-lived-picker-token"
        for call in http_calls
    )
    assert all(
        "short-lived-picker-token" not in json.dumps(params, default=str)
        for _query, params in sql_calls
    )
    assert any("last_used_at=now()" in query for query, _params in sql_calls)


def test_sheet_import_appends_private_frozen_surface_and_receipt():
    session_id = str(uuid.uuid4())
    sql_calls = []

    class Transaction:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def transaction(self):
            return Transaction()

        def execute(self, query, params=None):
            sql_calls.append((query, params))
            if query.startswith("SELECT * FROM rvbbit.calliope_sessions"):
                return _Result({"id": session_id, "title": "Planning"})
            if query.startswith("SELECT 1 AS found"):
                return _Result()
            if query.startswith("SELECT * FROM rvbbit.calliope_google_imports"):
                return _Result()
            if query.startswith("SELECT coalesce(max(ordinal)"):
                return _Result({"n": 3})
            if query.startswith("INSERT INTO rvbbit.calliope_turns"):
                return _Result({
                    "id": params[0],
                    "session_id": session_id,
                    "ordinal": params[2],
                    "user_message": params[3],
                    "assistant_message": params[4],
                    "status": "complete",
                    "turn_kind": "sheet_import",
                })
            if query.startswith("SELECT id FROM rvbbit.calliope_surfaces"):
                return _Result({"id": str(uuid.uuid4())})
            if query.startswith("INSERT INTO rvbbit.calliope_surfaces"):
                return _Result({
                    "id": params[0],
                    "session_id": session_id,
                    "turn_id": params[2],
                    "ordinal": 1,
                    "kind": "query",
                    "title": params[3],
                    "tool_name": "google_sheet_import",
                    "tool_call_id": params[4],
                    "lineage_key": params[5],
                    "parent_surface_id": params[6],
                    "payload": json.loads(params[7]),
                    "source": json.loads(params[8]),
                    "presentation": json.loads(params[9]),
                })
            if query.startswith("INSERT INTO rvbbit.calliope_google_imports"):
                return _Result({
                    "id": params[0],
                    "owner_email": params[1],
                    "session_id": params[2],
                    "surface_id": params[3],
                    "provider": "google_sheets",
                    "provider_file_id": params[4],
                    "provider_sheet_id": params[5],
                    "spreadsheet_title": params[6],
                    "sheet_name": params[7],
                    "selected_range": params[8],
                    "row_count": params[10],
                    "column_count": params[11],
                    "snapshot_hash": params[12],
                })
            if query.startswith("UPDATE rvbbit.calliope_sessions"):
                return _Result({"id": session_id, "title": "Planning"})
            raise AssertionError(query)

    result = calliope.store_google_sheet_import(
        Connection,
        "owner@example.com",
        session_id,
        {
            "workbook": {
                "id": "sheet_file_12345",
                "title": "FY Plan",
                "url": "https://docs.google.com/spreadsheets/d/sheet_file_12345/edit",
            },
            "sheet": {"id": 7, "title": "Pipeline"},
            "selected_range": "A1:B3",
            "resolved_range": "'Pipeline'!A1:B3",
            "first_row_header": True,
            "columns": [{"name": "Owner", "type": "text"}, {"name": "Amount", "type": "integer"}],
            "rows": [{"Owner": "Ada", "Amount": 12}, {"Owner": "Lin", "Amount": 9}],
            "truncated": False,
            "snapshot_hash": "a" * 64,
        },
    )

    owner_select = next(query for query, _params in sql_calls if query.startswith(
        "SELECT * FROM rvbbit.calliope_sessions"
    ))
    assert "lower(owner_email)=lower(%s)" in owner_select
    assert result["turn"]["turn_kind"] == "sheet_import"
    assert result["surface"]["source"]["origin"] == "google_sheet_import"
    assert result["surface"]["payload"]["rows"][0]["Owner"] == "Ada"
    assert result["surface"]["presentation"]["google_sheet"]["source"] is True
    assert result["import"]["owner_email"] == "owner@example.com"
    assert result["changed"] is True
    assert result["operation"] == "import"
    assert "source" not in result["import"]


def test_sheet_refresh_without_changes_updates_freshness_without_a_new_turn():
    session_id = str(uuid.uuid4())
    surface_id = str(uuid.uuid4())
    import_id = str(uuid.uuid4())
    sql_calls = []

    class Transaction:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def transaction(self):
            return Transaction()

        def execute(self, query, params=None):
            sql_calls.append((query, params))
            if query.startswith("SELECT * FROM rvbbit.calliope_sessions"):
                return _Result({"id": session_id, "title": "Planning"})
            if query.startswith("SELECT 1 AS found"):
                return _Result()
            if query.startswith("SELECT * FROM rvbbit.calliope_google_imports"):
                return _Result({
                    "id": import_id,
                    "session_id": session_id,
                    "surface_id": surface_id,
                    "snapshot_hash": "a" * 64,
                    "status": "active",
                })
            if query.startswith("UPDATE rvbbit.calliope_google_imports"):
                return _Result({
                    "id": import_id,
                    "session_id": session_id,
                    "surface_id": surface_id,
                    "snapshot_hash": "a" * 64,
                    "status": "active",
                })
            if query.startswith("UPDATE rvbbit.calliope_surfaces"):
                return _Result({
                    "id": surface_id,
                    "session_id": session_id,
                    "kind": "query",
                    "title": "FY Plan · Pipeline",
                    "payload": {
                        "snapshot_hash": "a" * 64,
                        "lifecycle_status": "active",
                        "sync_status": "current",
                    },
                    "source": {"origin": "google_sheet_import"},
                    "presentation": {},
                })
            if query.startswith("UPDATE rvbbit.calliope_sessions"):
                return _Result({"id": session_id, "title": "Planning"})
            raise AssertionError(query)

    result = calliope.store_google_sheet_import(
        Connection,
        "owner@example.com",
        session_id,
        _sheet_snapshot_for_lifecycle("a" * 64),
        operation="refresh",
        expected_import_id=import_id,
    )

    assert result["changed"] is False
    assert result["operation"] == "check"
    assert result["turn"] is None
    assert result["surface"]["id"] == surface_id
    assert not any(query.startswith("INSERT INTO rvbbit.calliope_turns") for query, _ in sql_calls)


def _sheet_snapshot_for_lifecycle(snapshot_hash):
    return {
        "workbook": {
            "id": "sheet_file_12345",
            "title": "FY Plan",
            "url": "https://docs.google.com/spreadsheets/d/sheet_file_12345/edit",
        },
        "sheet": {"id": 7, "title": "Pipeline"},
        "selected_range": "A1:B3",
        "resolved_range": "'Pipeline'!A1:B3",
        "first_row_header": True,
        "columns": [
            {"name": "Owner", "type": "text", "sql_name": "owner"},
            {"name": "Amount", "type": "integer", "sql_name": "amount"},
        ],
        "rows": [{"Owner": "Ada", "Amount": 12}],
        "truncated": False,
        "snapshot_hash": snapshot_hash,
    }


def test_changed_sheet_refresh_supersedes_and_links_an_immutable_revision():
    session_id = str(uuid.uuid4())
    old_surface_id = str(uuid.uuid4())
    old_import_id = str(uuid.uuid4())
    sql_calls = []

    class Transaction:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def transaction(self):
            return Transaction()

        def execute(self, query, params=None):
            sql_calls.append((query, params))
            if query.startswith("SELECT * FROM rvbbit.calliope_sessions"):
                return _Result({"id": session_id, "title": "Planning"})
            if query.startswith("SELECT 1 AS found"):
                return _Result()
            if query.startswith("SELECT * FROM rvbbit.calliope_google_imports"):
                return _Result({
                    "id": old_import_id,
                    "session_id": session_id,
                    "surface_id": old_surface_id,
                    "snapshot_hash": "a" * 64,
                    "status": "active",
                })
            if query.startswith("UPDATE rvbbit.calliope_google_imports"):
                return _Result()
            if query.startswith("UPDATE rvbbit.calliope_surfaces"):
                return _Result()
            if query.startswith("SELECT coalesce(max(ordinal)"):
                return _Result({"n": 4})
            if query.startswith("INSERT INTO rvbbit.calliope_turns"):
                return _Result({
                    "id": params[0], "session_id": session_id, "ordinal": params[2],
                    "user_message": params[3], "assistant_message": params[4],
                    "status": "complete", "turn_kind": params[5],
                })
            if query.startswith("INSERT INTO rvbbit.calliope_surfaces"):
                return _Result({
                    "id": params[0], "session_id": session_id, "turn_id": params[2],
                    "ordinal": 1, "kind": "query", "title": params[3],
                    "tool_name": "google_sheet_import", "tool_call_id": params[4],
                    "lineage_key": params[5], "parent_surface_id": params[6],
                    "payload": json.loads(params[7]), "source": json.loads(params[8]),
                    "presentation": json.loads(params[9]),
                })
            if query.startswith("INSERT INTO rvbbit.calliope_google_imports"):
                return _Result({
                    "id": params[0], "owner_email": params[1], "session_id": params[2],
                    "surface_id": params[3], "snapshot_hash": params[12],
                    "operation": params[13], "status": "active",
                })
            if query.startswith("UPDATE rvbbit.calliope_sessions"):
                return _Result({"id": session_id, "title": "Planning"})
            raise AssertionError(query)

    result = calliope.store_google_sheet_import(
        Connection,
        "owner@example.com",
        session_id,
        _sheet_snapshot_for_lifecycle("b" * 64),
        operation="refresh",
        expected_import_id=old_import_id,
    )

    assert result["changed"] is True
    assert result["operation"] == "refresh"
    assert result["turn"]["turn_kind"] == "sheet_refresh"
    assert result["surface"]["parent_surface_id"] == old_surface_id
    assert result["surface"]["payload"]["snapshot_hash"] == "b" * 64
    assert any("status='superseded'" in query for query, _ in sql_calls)


def test_google_document_parser_preserves_tabs_headings_lists_and_tables():
    snapshot = calliope._google_document_snapshot(
        {
            "documentId": "document_file_12345",
            "title": "Operating plan",
            "revisionId": "rev-7",
            "tabs": [{
                "tabProperties": {"title": "Plan"},
                "documentTab": {"body": {"content": [
                    {"paragraph": {
                        "paragraphStyle": {"namedStyleType": "HEADING_1"},
                        "elements": [{"textRun": {"content": "Priorities\n"}}],
                    }},
                    {"paragraph": {
                        "bullet": {"nestingLevel": 0},
                        "elements": [{"textRun": {"content": "Protect retention\n"}}],
                    }},
                    {"table": {"tableRows": [{"tableCells": [
                        {"content": [{"paragraph": {"elements": [
                            {"textRun": {"content": "Owner\n"}},
                        ]}}]},
                        {"content": [{"paragraph": {"elements": [
                            {"textRun": {"content": "Abby\n"}},
                        ]}}]},
                    ]}] }},
                ]}},
                "childTabs": [{
                    "tabProperties": {"title": "Risks"},
                    "documentTab": {"body": {"content": [{"paragraph": {
                        "elements": [{"textRun": {"content": "Capacity is tight.\n"}}],
                    }}]}},
                }],
            }],
        },
        "document_file_12345",
    )

    assert snapshot["title"] == "Operating plan"
    assert snapshot["tab_count"] == 2
    assert snapshot["tab_titles"] == ["Plan", "Risks"]
    assert "# Plan" in snapshot["body"]
    assert "# Priorities" in snapshot["body"]
    assert "- Protect retention" in snapshot["body"]
    assert "| Owner | Abby |" in snapshot["body"]
    assert "## Risks" in snapshot["body"]
    assert snapshot["revision_id"] == "rev-7"
    assert len(snapshot["content_hash"]) == 64


def test_google_document_inspection_reads_only_picker_file_and_keeps_token_ephemeral(monkeypatch):
    sql_calls = []
    http_calls = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params=None):
            sql_calls.append((query, params))
            return _Result()

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "documentId": "document_file_12345",
                "title": "Operating plan",
                "revisionId": "rev-7",
                "body": {"content": [{"paragraph": {"elements": [
                    {"textRun": {"content": "The operating plan.\n"}},
                ]}}]},
            }

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, url, **kwargs):
            http_calls.append((url, kwargs))
            return Response()

    async def token(*_args):
        return "short-lived-doc-token"

    monkeypatch.setattr(calliope, "_google_workspace_access_token", token)
    monkeypatch.setattr(calliope.httpx, "AsyncClient", Client)

    result = asyncio.run(calliope.inspect_google_document(
        Connection,
        "owner@example.com",
        "document_file_12345",
    ))

    assert result["title"] == "Operating plan"
    assert result["body"] == "The operating plan."
    assert http_calls == [(
        "https://docs.googleapis.com/v1/documents/document_file_12345",
        {
            "headers": {"Authorization": "Bearer short-lived-doc-token"},
            "params": {"includeTabsContent": "true"},
        },
    )]
    assert all(
        "short-lived-doc-token" not in json.dumps(params, default=str)
        for _query, params in sql_calls
    )
    assert any("last_used_at=now()" in query for query, _params in sql_calls)


def test_google_document_import_grants_only_owner_role_and_indexes_brain_doc():
    session_id = str(uuid.uuid4())
    parent_id = str(uuid.uuid4())
    sql_calls = []

    class Transaction:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def transaction(self):
            return Transaction()

        def execute(self, query, params=None):
            sql_calls.append((query, params))
            if query.startswith("SELECT * FROM rvbbit.calliope_sessions"):
                return _Result({"id": session_id, "title": "Planning"})
            if query.startswith("SELECT 1 AS found"):
                return _Result()
            if query.startswith("SELECT * FROM rvbbit.calliope_google_document_imports"):
                return _Result()
            if query.startswith("SELECT coalesce(max(ordinal)"):
                return _Result({"n": 4})
            if query.startswith("INSERT INTO rvbbit.brain_roles"):
                return _Result()
            if query.startswith("INSERT INTO rvbbit.brain_role_members"):
                return _Result()
            if query.startswith("SELECT rvbbit.brain_ingest"):
                return _Result({"doc_id": 404})
            if query.startswith("UPDATE rvbbit.brain_documents"):
                return _Result()
            if query.startswith("UPDATE rvbbit.brain_sources"):
                return _Result()
            if query.startswith("INSERT INTO rvbbit.calliope_turns"):
                return _Result({
                    "id": params[0],
                    "session_id": session_id,
                    "ordinal": params[2],
                    "user_message": params[3],
                    "assistant_message": params[4],
                    "status": "complete",
                    "turn_kind": "document_import",
                })
            if query.startswith("SELECT id FROM rvbbit.calliope_surfaces"):
                return _Result({"id": parent_id})
            if query.startswith("INSERT INTO rvbbit.calliope_surfaces"):
                return _Result({
                    "id": params[0],
                    "session_id": session_id,
                    "turn_id": params[2],
                    "ordinal": 1,
                    "kind": "document",
                    "title": params[3],
                    "tool_name": "google_document_import",
                    "tool_call_id": params[4],
                    "lineage_key": params[5],
                    "parent_surface_id": params[6],
                    "payload": json.loads(params[7]),
                    "source": json.loads(params[8]),
                    "presentation": json.loads(params[9]),
                })
            if query.startswith("INSERT INTO rvbbit.calliope_google_document_imports"):
                return _Result({
                    "id": params[0],
                    "owner_email": params[1],
                    "session_id": params[2],
                    "surface_id": params[3],
                    "brain_doc_id": params[4],
                    "private_role": params[5],
                    "provider": "google_docs",
                    "provider_file_id": params[6],
                    "document_title": params[7],
                    "revision_id": params[8],
                    "character_count": params[9],
                    "word_count": params[10],
                    "tab_count": params[11],
                    "content_hash": params[12],
                    "operation": params[13],
                    "status": "active",
                    "created_at": "2026-08-05T12:00:00Z",
                })
            if query.startswith("UPDATE rvbbit.calliope_sessions"):
                return _Result({"id": session_id, "title": "Planning"})
            raise AssertionError(query)

    owner = "Owner@Example.com"
    result = calliope.store_google_document_import(
        Connection,
        owner,
        session_id,
        {
            "id": "document_file_12345",
            "title": "Operating plan",
            "url": "https://docs.google.com/document/d/document_file_12345/edit",
            "revision_id": "rev-7",
            "body": "# Operating plan\n\nProtect retention.",
            "excerpt": "# Operating plan\n\nProtect retention.",
            "character_count": 37,
            "word_count": 5,
            "tab_count": 1,
            "tab_titles": [],
            "content_hash": "b" * 64,
        },
    )

    role = calliope._google_private_brain_role(owner)
    assert "owner@example.com" not in role
    member_call = next(
        params for query, params in sql_calls
        if query.startswith("INSERT INTO rvbbit.brain_role_members")
    )
    assert member_call == (role, "owner@example.com")
    ingest_call = next(
        params for query, params in sql_calls
        if query.startswith("SELECT rvbbit.brain_ingest")
    )
    assert ingest_call[3] == [role]
    assert "owner@example.com" not in ingest_call[5]
    assert ingest_call[5].endswith(":document_file_12345")
    assert result["turn"]["turn_kind"] == "document_import"
    assert result["surface"]["kind"] == "document"
    assert result["surface"]["payload"]["brain_doc_id"] == 404
    assert result["surface"]["payload"]["private"] is True
    assert result["surface"]["payload"]["lifecycle_status"] == "active"
    assert result["operation"] == "import"
    assert result["changed"] is True
    assert result["surface"]["source"]["origin"] == "google_document_import"
    assert result["import"]["owner_email"] == "owner@example.com"
    assert "private_role" not in result["import"]


def test_google_document_refresh_is_quiet_when_content_is_current():
    session_id = str(uuid.uuid4())
    surface_id = str(uuid.uuid4())
    import_id = str(uuid.uuid4())
    sql_calls = []
    active = {
        "id": import_id,
        "owner_email": "owner@example.com",
        "session_id": session_id,
        "surface_id": surface_id,
        "brain_doc_id": 404,
        "private_role": calliope._google_private_brain_role("owner@example.com"),
        "provider_file_id": "document_file_12345",
        "document_title": "Operating plan",
        "revision_id": "rev-7",
        "content_hash": "b" * 64,
        "status": "active",
        "created_at": "2026-08-05T12:00:00Z",
    }

    class Transaction:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def transaction(self):
            return Transaction()

        def execute(self, query, params=None):
            sql_calls.append((query, params))
            if query.startswith("SELECT * FROM rvbbit.calliope_sessions"):
                return _Result({"id": session_id, "title": "Planning"})
            if query.startswith("SELECT 1 AS found"):
                return _Result()
            if query.startswith("SELECT * FROM rvbbit.calliope_google_document_imports"):
                return _Result(active)
            if query.startswith("UPDATE rvbbit.calliope_google_document_imports SET last_checked_at"):
                return _Result({**active, "revision_id": params[0]})
            if query.startswith("UPDATE rvbbit.calliope_surfaces SET payload"):
                return _Result({
                    "id": surface_id,
                    "session_id": session_id,
                    "kind": "document",
                    "title": "Operating plan",
                    "payload": json.loads(params[0]),
                    "source": {"origin": "google_document_import"},
                })
            if query.startswith("UPDATE rvbbit.calliope_sessions"):
                return _Result({"id": session_id, "title": "Planning"})
            raise AssertionError(query)

    result = calliope.store_google_document_import(
        Connection,
        "owner@example.com",
        session_id,
        {
            "id": "document_file_12345",
            "title": "Operating plan",
            "url": "https://docs.google.com/document/d/document_file_12345/edit",
            "revision_id": "rev-8",
            "body": "The content is unchanged.",
            "excerpt": "The content is unchanged.",
            "content_hash": "b" * 64,
        },
        operation="refresh",
        expected_import_id=import_id,
    )

    assert result["changed"] is False
    assert result["operation"] == "check"
    assert result["turn"] is None
    assert result["surface"]["payload"]["sync_status"] == "current"
    assert not any("brain_ingest" in query for query, _params in sql_calls)
    assert not any(query.startswith("INSERT INTO rvbbit.calliope_turns") for query, _params in sql_calls)


def test_google_document_refresh_supersedes_receipt_and_links_new_stage_revision():
    session_id = str(uuid.uuid4())
    previous_surface_id = str(uuid.uuid4())
    previous_import_id = str(uuid.uuid4())
    sql_calls = []
    active = {
        "id": previous_import_id,
        "owner_email": "owner@example.com",
        "session_id": session_id,
        "surface_id": previous_surface_id,
        "brain_doc_id": 404,
        "private_role": calliope._google_private_brain_role("owner@example.com"),
        "provider_file_id": "document_file_12345",
        "document_title": "Operating plan",
        "content_hash": "a" * 64,
        "status": "active",
        "created_at": "2026-08-05T12:00:00Z",
    }

    class Transaction:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def transaction(self):
            return Transaction()

        def execute(self, query, params=None):
            sql_calls.append((query, params))
            if query.startswith("SELECT * FROM rvbbit.calliope_sessions"):
                return _Result({"id": session_id, "title": "Planning"})
            if query.startswith("SELECT 1 AS found"):
                return _Result()
            if query.startswith("SELECT * FROM rvbbit.calliope_google_document_imports"):
                return _Result(active)
            if query.startswith("SELECT coalesce(max(ordinal)"):
                return _Result({"n": 5})
            if query.startswith((
                "INSERT INTO rvbbit.brain_roles",
                "INSERT INTO rvbbit.brain_role_members",
                "UPDATE rvbbit.calliope_google_document_imports SET status",
                "UPDATE rvbbit.calliope_surfaces SET payload",
                "UPDATE rvbbit.brain_documents",
                "UPDATE rvbbit.brain_sources",
            )):
                return _Result()
            if query.startswith("SELECT rvbbit.brain_ingest"):
                return _Result({"doc_id": 404})
            if query.startswith("INSERT INTO rvbbit.calliope_turns"):
                return _Result({
                    "id": params[0], "session_id": session_id, "ordinal": params[2],
                    "user_message": params[3], "assistant_message": params[4],
                    "status": "complete", "turn_kind": params[5],
                })
            if query.startswith("SELECT id FROM rvbbit.calliope_surfaces"):
                return _Result({"id": previous_surface_id})
            if query.startswith("INSERT INTO rvbbit.calliope_surfaces"):
                return _Result({
                    "id": params[0], "session_id": session_id, "turn_id": params[2],
                    "ordinal": 1, "kind": "document", "title": params[3],
                    "tool_name": "google_document_import", "tool_call_id": params[4],
                    "lineage_key": params[5], "parent_surface_id": params[6],
                    "payload": json.loads(params[7]), "source": json.loads(params[8]),
                    "presentation": json.loads(params[9]),
                })
            if query.startswith("INSERT INTO rvbbit.calliope_google_document_imports"):
                return _Result({
                    "id": params[0], "owner_email": params[1], "session_id": params[2],
                    "surface_id": params[3], "brain_doc_id": params[4],
                    "private_role": params[5], "provider_file_id": params[6],
                    "document_title": params[7], "revision_id": params[8],
                    "character_count": params[9], "word_count": params[10],
                    "tab_count": params[11], "content_hash": params[12],
                    "operation": params[13], "status": "active",
                })
            if query.startswith("UPDATE rvbbit.calliope_sessions"):
                return _Result({"id": session_id, "title": "Planning"})
            raise AssertionError(query)

    result = calliope.store_google_document_import(
        Connection,
        "owner@example.com",
        session_id,
        {
            "id": "document_file_12345",
            "title": "Operating plan",
            "url": "https://docs.google.com/document/d/document_file_12345/edit",
            "revision_id": "rev-8",
            "body": "A materially changed operating plan.",
            "excerpt": "A materially changed operating plan.",
            "content_hash": "b" * 64,
        },
        operation="refresh",
        expected_import_id=previous_import_id,
    )

    assert result["changed"] is True
    assert result["operation"] == "refresh"
    assert result["turn"]["turn_kind"] == "document_refresh"
    assert result["surface"]["parent_surface_id"] == previous_surface_id
    assert result["surface"]["payload"]["lifecycle_status"] == "active"
    supersede = next(
        params for query, params in sql_calls
        if query.startswith("UPDATE rvbbit.calliope_google_document_imports SET status")
    )
    assert supersede == (previous_import_id,)


def test_google_document_forget_scrubs_brain_content_but_preserves_drive_original():
    session_id = str(uuid.uuid4())
    surface_id = str(uuid.uuid4())
    import_id = str(uuid.uuid4())
    private_role = calliope._google_private_brain_role("owner@example.com")
    sql_calls = []
    active = {
        "id": import_id,
        "owner_email": "owner@example.com",
        "session_id": session_id,
        "surface_id": surface_id,
        "brain_doc_id": 404,
        "private_role": private_role,
        "provider_file_id": "document_file_12345",
        "document_title": "Operating plan",
        "character_count": 40,
        "word_count": 6,
        "tab_count": 1,
        "content_hash": "b" * 64,
        "status": "active",
        "source": {
            "source_url": "https://docs.google.com/document/d/document_file_12345/edit",
        },
    }

    class Transaction:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def transaction(self):
            return Transaction()

        def execute(self, query, params=None):
            sql_calls.append((query, params))
            if query.startswith("SELECT * FROM rvbbit.calliope_sessions"):
                return _Result({"id": session_id, "title": "Planning"})
            if query.startswith("SELECT 1 AS found FROM rvbbit.calliope_turns"):
                return _Result()
            if query.startswith("SELECT * FROM rvbbit.calliope_google_document_imports"):
                return _Result(active)
            if query.startswith("SELECT 1 AS found FROM rvbbit.brain_doc_roles"):
                return _Result({"found": 1})
            if query.startswith("SELECT coalesce(max(ordinal)"):
                return _Result({"n": 6})
            if query.startswith((
                "DELETE FROM rvbbit.kg_nodes",
                "DELETE FROM rvbbit.brain_chunks",
                "DELETE FROM rvbbit.brain_doc_roles",
                "DELETE FROM rvbbit.brain_doc_exclude",
                "UPDATE rvbbit.brain_documents",
            )):
                return _Result()
            if query.startswith("UPDATE rvbbit.calliope_google_document_imports"):
                return _Result({**active, "status": "removed"})
            if query.startswith("UPDATE rvbbit.calliope_surfaces SET payload"):
                return _Result({
                    "id": surface_id, "session_id": session_id,
                    "lineage_key": "google-doc:lineage", "kind": "document",
                    "title": "Operating plan", "payload": json.loads(params[0]),
                })
            if query.startswith("INSERT INTO rvbbit.calliope_turns"):
                return _Result({
                    "id": params[0], "session_id": session_id, "ordinal": params[2],
                    "user_message": params[3], "assistant_message": params[4],
                    "status": "complete", "turn_kind": "document_remove",
                })
            if query.startswith("INSERT INTO rvbbit.calliope_surfaces"):
                return _Result({
                    "id": params[0], "session_id": session_id, "turn_id": params[2],
                    "ordinal": 1, "kind": "document", "title": params[3],
                    "tool_name": "google_document_remove", "tool_call_id": params[4],
                    "lineage_key": params[5], "parent_surface_id": params[6],
                    "payload": json.loads(params[7]), "source": json.loads(params[8]),
                    "presentation": json.loads(params[9]),
                })
            if query.startswith("UPDATE rvbbit.calliope_sessions"):
                return _Result({"id": session_id, "title": "Planning"})
            raise AssertionError(query)

    result = calliope.remove_google_document_import(
        Connection, "owner@example.com", session_id, surface_id,
    )

    assert result["removed"] is True
    assert result["turn"]["turn_kind"] == "document_remove"
    assert result["surface"]["payload"]["indexed"] is False
    assert result["surface"]["payload"]["lifecycle_status"] == "removed"
    assert result["surface"]["payload"]["download_url"].startswith("https://docs.google.com/")
    assert any(
        query.startswith("UPDATE rvbbit.brain_documents SET body=NULL,deleted_at=now()")
        for query, _params in sql_calls
    )
    assert any(query.startswith("DELETE FROM rvbbit.brain_chunks") for query, _params in sql_calls)


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
