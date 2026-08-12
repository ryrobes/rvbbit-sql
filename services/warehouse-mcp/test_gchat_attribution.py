"""Legacy Hermes forwarding stays attribution-only and turn-scoped where private."""
from __future__ import annotations

import asyncio
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

from mcp import types as mcp_types
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session


_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import server  # noqa: E402
import calliope  # noqa: E402


def _context(
    email="analyst@example.com",
    *,
    platform="google_chat",
    session_id="gchat:spaces/abc:analyst@example.com",
):
    metadata = {
        server._HERMES_CALLER_META_KEY: {
            "source": "hermes",
            "platform": platform,
            "user_id": email,
            "session_id": session_id,
        }
    }
    return SimpleNamespace(
        request_context=SimpleNamespace(
            request=SimpleNamespace(root=SimpleNamespace(params={"_meta": metadata}))
        )
    )


def test_static_hermes_publication_uses_verified_gchat_email(monkeypatch):
    monkeypatch.setattr(server, "_authenticated_caller", lambda: ("calliope@example.com", "static-key"))
    monkeypatch.setattr(server, "_logged", lambda _tool, _args, fn: fn())
    monkeypatch.setattr(
        server,
        "tool_publish_dashboard",
        lambda *_args, **_kwargs: {"caller": server._caller()},
    )

    result = server._mcp_publish_dashboard(
        "Chat-created dashboard", html="<main>ok</main>", ctx=_context("Person@Example.com")
    )

    assert result["caller"] == ("person@example.com", "static-key")
    assert server._FORWARDED_CALLER.get() is None


def test_static_hermes_api_publication_uses_signed_calliope_session_owner(monkeypatch):
    observed = {}
    monkeypatch.setattr(
        server,
        "_authenticated_caller",
        lambda: ("calliope@example.com", "static-key"),
    )
    monkeypatch.setattr(server, "_conn", lambda: nullcontext(object()))

    def linked(_conn, session_ref):
        observed["session_ref"] = session_ref
        return {
            "owner": "person@example.com",
            "calliope_session_id": "755303d5-7f91-42c3-bbea-b989e34a56c9",
            "kind": "session",
        }

    monkeypatch.setattr(server, "_calliope_activity_for_hermes_session", linked)
    monkeypatch.setattr(server, "_logged", lambda _tool, _args, fn: fn())
    monkeypatch.setattr(
        server,
        "tool_publish_dashboard",
        lambda *_args, **_kwargs: {"caller": server._caller()},
    )

    result = server._mcp_publish_dashboard(
        "Web-created dashboard",
        html="<main>ok</main>",
        ctx=_context(
            "forged@example.com",
            platform="api_server",
            session_id="calliope_123",
        ),
    )

    assert observed["session_ref"] == "calliope_123"
    assert result["caller"] == ("person@example.com", "static-key")
    assert server._FORWARDED_CALLER.get() is None


def test_forwarded_identity_never_overrides_oauth_caller(monkeypatch):
    monkeypatch.setattr(server, "_authenticated_caller", lambda: ("oauth@example.com", "oauth-client"))

    observed = server._with_forwarded_mcp_caller(
        _context("other@example.com"), server._caller
    )

    assert observed == ("oauth@example.com", "oauth-client")


def test_openrouter_requests_carry_calliope_app_and_human_attribution(monkeypatch):
    monkeypatch.setattr(server, "_request_tracking_user", lambda: "person@example.com")

    headers, body = server._openai_compatible_request(
        "https://openrouter.ai/api/v1",
        "secret",
        {"model": "example/model", "messages": []},
    )

    assert headers["HTTP-Referer"] == "https://rvbbit.ai"
    assert headers["X-OpenRouter-Title"] == "Calliope (RVBBIT)"
    assert headers["X-Title"] == "Calliope (RVBBIT)"
    assert body["user"] == "person@example.com"


def test_non_openrouter_compatible_request_does_not_add_vendor_fields(monkeypatch):
    monkeypatch.setattr(server, "_request_tracking_user", lambda: "person@example.com")

    headers, body = server._openai_compatible_request(
        "https://api.openai.com/v1",
        "secret",
        {"model": "example/model", "messages": []},
    )

    assert set(headers) == {"Authorization"}
    assert "user" not in body


def test_background_openrouter_request_has_stable_system_user(monkeypatch):
    monkeypatch.setattr(server, "_request_tracking_user", lambda: None)

    _headers, body = server._openai_compatible_request(
        "https://openrouter.ai/api/v1",
        "secret",
        {"model": "example/model", "messages": []},
    )

    assert body["user"] == "calliope-system"


def test_sql_connection_tracks_caller_for_pg_rvbbit_without_authorizing(monkeypatch):
    observed = []

    class Connection:
        def execute(self, query, params):
            observed.append((query, params))

    monkeypatch.setattr(server, "_request_tracking_user", lambda: "person@example.com")
    server._set_request_tracking_user(Connection())

    assert observed == [(
        "SELECT set_config('rvbbit.request_user', %s, false)",
        ("person@example.com",),
    )]


def test_selected_private_document_read_uses_exact_running_calliope_turn(monkeypatch):
    observed = {}

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params):
            observed["query"] = query
            observed["params"] = params
            return SimpleNamespace(
                fetchone=lambda: {"owner_email": "Person@Example.com"}
            )

    monkeypatch.setattr(
        server,
        "_authenticated_caller",
        lambda: ("calliope@example.com", "static-key"),
    )
    monkeypatch.setattr(server, "_conn", Connection)
    monkeypatch.setattr(server, "_logged", lambda _tool, _args, fn: fn())
    monkeypatch.setattr(
        server,
        "tool_brain_get_doc",
        lambda doc_id, caller: {"doc_id": doc_id, "caller": caller},
    )

    result = server._mcp_brain_get_doc(
        "102892",
        ctx=_context(
            "forged@example.com",
            platform="api_server",
            session_id="calliope_private_turn",
        ),
    )

    assert result == {"doc_id": "102892", "caller": "person@example.com"}
    assert observed["params"] == ("calliope_private_turn", 102892)
    assert "t.status='running'" in observed["query"]
    assert "i.surface_id=t.selected_surface_id" in observed["query"]
    assert "i.status='active'" in observed["query"]


def test_private_document_scope_does_not_trust_unlinked_forwarded_identity(monkeypatch):
    monkeypatch.setattr(
        server,
        "_authenticated_caller",
        lambda: ("calliope@example.com", "static-key"),
    )

    assert server._selected_calliope_private_document_caller(
        102892,
        _context("person@example.com", platform="google_chat"),
    ) is None


def test_selected_private_sheet_reader_is_bounded_and_turn_scoped(monkeypatch):
    surface_id = "f8db6009-66e0-4471-85b9-06e704334431"
    observed = {}

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params):
            observed["query"] = query
            observed["params"] = params
            return SimpleNamespace(fetchone=lambda: {
                "surface_id": surface_id,
                "title": "Enrollment Pace · Fall 2026",
                "payload": {
                    "columns": [
                        {"name": "student", "type": "text"},
                        {"name": "pace", "type": "number"},
                        {"name": "note", "type": "text"},
                    ],
                    "rows": [
                        {"student": "Ada", "pace": 4, "note": "current"},
                        {"student": "Grace", "pace": 7, "note": "watch"},
                        {"student": "Linus", "pace": 6, "note": "current"},
                    ],
                },
                "owner_email": "person@example.com",
                "provider_file_id": "sheet-file-id",
                "provider_sheet_id": 123,
                "spreadsheet_title": "Enrollment Pace",
                "sheet_name": "Fall 2026",
                "selected_range": "A1:C4",
                "first_row_header": True,
                "row_count": 3,
                "column_count": 3,
                "snapshot_hash": "a" * 64,
                "created_at": "2026-08-05T20:00:00Z",
            })

    monkeypatch.setattr(
        server,
        "_authenticated_caller",
        lambda: ("calliope@example.com", "static-key"),
    )
    monkeypatch.setattr(server, "_conn", Connection)
    monkeypatch.setattr(server, "_record", lambda *_args, **_kwargs: None)

    result = server._mcp_calliope_sheet_snapshot(
        surface_id,
        offset=1,
        limit=1,
        column_offset=1,
        column_limit=2,
        ctx=_context(
            "forged@example.com",
            platform="api_server",
            session_id="calliope_private_turn",
        ),
    )

    assert observed["params"] == ("calliope_private_turn", surface_id)
    assert "f.id=t.selected_surface_id" in observed["query"]
    assert "t.status='running'" in observed["query"]
    assert result["private"] is True
    assert result["frozen_snapshot"] is True
    assert result["columns"] == [
        {
            "name": "pace",
            "sql_name": "pace",
            "type": "number",
            "sql_type": "double precision",
        },
        {"name": "note", "sql_name": "note", "type": "text", "sql_type": "text"},
    ]
    assert result["rows"] == [{"pace": 7, "note": "watch"}]
    assert result["next_offset"] == 2
    assert result["next_column_offset"] is None


def test_selected_private_sheet_query_joins_typed_snapshot_with_lineage(monkeypatch):
    surface_id = "f8db6009-66e0-4471-85b9-06e704334431"
    observed = {}
    selected = {
        "surface_id": surface_id,
        "title": "Pipeline Plan",
        "payload": {
            "columns": [
                {"name": "Account Owner", "type": "text"},
                {"name": "Order", "type": "integer"},
            ],
            "rows": [
                {"Account Owner": "Ada", "Order": 7},
                {"Account Owner": "Lin", "Order": ""},
            ],
        },
        "provider_file_id": "sheet-file-id",
        "provider_sheet_id": 12,
        "spreadsheet_title": "Pipeline Plan",
        "sheet_name": "Current",
        "selected_range": "A1:B3",
        "snapshot_hash": "b" * 64,
        "created_at": "2026-08-05T20:00:00Z",
    }

    class Cursor:
        description = [
            SimpleNamespace(name="account_owner", type_code=25),
            SimpleNamespace(name="segment", type_code=25),
        ]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement):
            observed["execution_sql"] = statement.as_string()

        def fetchmany(self, count):
            observed["fetchmany"] = count
            return [
                {"account_owner": "Ada", "segment": "Enterprise"},
                {"account_owner": "Lin", "segment": "SMB"},
            ]

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def cursor():
            return Cursor()

    def validate(statement, as_of):
        observed["validation_sql"] = statement
        observed["as_of"] = as_of
        return {
            "valid": True,
            "safe_select": True,
            "engine": "postgres",
            "rvbbit_tables": [],
        }

    monkeypatch.setattr(server, "_selected_calliope_sheet_row", lambda *_args: selected)
    monkeypatch.setattr(server, "tool_validate_sql", validate)
    monkeypatch.setattr(
        server, "_referenced_tables", lambda statement: ["public.accounts"]
    )
    monkeypatch.setattr(server, "_session_pg_role", lambda: None)
    monkeypatch.setattr(server, "_conn", lambda **_kwargs: Connection())
    monkeypatch.setattr(server, "_logged", lambda _tool, _args, fn: fn())

    result = server._mcp_calliope_sheet_query(
        surface_id,
        "SELECT s.account_owner, a.segment FROM selected_sheet s "
        "LEFT JOIN public.accounts a ON lower(a.owner)=lower(s.account_owner)",
        limit=1,
    )

    assert "CAST('[]' AS jsonb)" in observed["validation_sql"]
    assert "Ada" not in observed["validation_sql"]
    assert '"account_owner" text' in observed["validation_sql"]
    assert '"sheet_order" bigint' in observed["validation_sql"]
    assert '"account_owner":"Ada"' in observed["execution_sql"]
    assert '"sheet_order":null' in observed["execution_sql"]
    assert observed["fetchmany"] == 2
    assert result["rows"] == [{"account_owner": "Ada", "segment": "Enterprise"}]
    assert result["truncated"] is True
    assert result["warehouse_objects"] == ["public.accounts"]
    assert result["sheet"]["relation"] == "selected_sheet"
    assert result["sheet"]["snapshot_hash"] == "b" * 64
    assert result["sheet"]["read_mode"] == "snapshot"
    assert result["lineage"]["sheet_surface_id"] == surface_id


def test_run_sql_compatibly_binds_only_the_active_selected_sheet(monkeypatch):
    surface_id = "f8db6009-66e0-4471-85b9-06e704334431"
    observed = {}

    monkeypatch.setattr(
        server,
        "_active_calliope_sheet_surface_id",
        lambda ctx=None: surface_id if ctx == "active-context" else None,
    )

    def sheet_query(selected_id, sql, **kwargs):
        observed.update(surface_id=selected_id, sql=sql, kwargs=kwargs)
        return {"rows": [{"joined": True}], "sheet": {"surface_id": selected_id}}

    monkeypatch.setattr(server, "_mcp_calliope_sheet_query", sheet_query)
    monkeypatch.setattr(
        server,
        "_logged",
        lambda tool, _args, fn: observed.update(ordinary_tool=tool) or fn(),
    )
    monkeypatch.setattr(
        server,
        "tool_run_sql",
        lambda *_args: {"rows": [{"ordinary": True}]},
    )

    joined = server._mcp_run_sql(
        "SELECT * FROM selected_sheet s JOIN public.accounts a ON true",
        limit=25,
        ctx="active-context",
    )
    assert joined["sheet"]["surface_id"] == surface_id
    assert observed == {
        "surface_id": surface_id,
        "sql": "SELECT * FROM selected_sheet s JOIN public.accounts a ON true",
        "kwargs": {
            "as_of": None,
            "limit": 25,
            "read_mode": "snapshot",
            "default_view": "table",
            "ctx": "active-context",
        },
    }

    # A schema-qualified warehouse table is not the reserved transient relation.
    ordinary = server._mcp_run_sql(
        "SELECT * FROM public.selected_sheet",
        ctx="active-context",
    )
    assert ordinary == {"rows": [{"ordinary": True}]}
    assert observed["ordinary_tool"] == "run_sql"

    quoted = server._mcp_run_sql(
        'SELECT * FROM "selected_sheet"',
        ctx="active-context",
    )
    assert quoted["sheet"]["surface_id"] == surface_id


def test_run_sql_multi_compatibly_binds_selected_sheet_queries(monkeypatch):
    surface_id = "f8db6009-66e0-4471-85b9-06e704334431"
    observed = {"sheet": [], "ordinary": []}

    monkeypatch.setattr(
        server, "_active_calliope_sheet_surface_id", lambda _ctx=None: surface_id
    )

    def sheet_query(selected_id, sql, **kwargs):
        observed["sheet"].append((selected_id, sql, kwargs))
        return {
            "columns": [{"name": "joined"}],
            "rows": [{"joined": True}],
            "row_count": 1,
            "truncated": False,
            "sheet": {"surface_id": selected_id, "read_mode": "snapshot"},
        }

    def ordinary_query(sql, as_of, limit):
        observed["ordinary"].append((sql, as_of, limit))
        return {
            "columns": [{"name": "ordinary"}],
            "rows": [{"ordinary": True}],
            "row_count": 1,
            "truncated": False,
        }

    monkeypatch.setattr(server, "_mcp_calliope_sheet_query", sheet_query)
    monkeypatch.setattr(server, "tool_run_sql", ordinary_query)
    monkeypatch.setattr(server, "_logged", lambda _tool, _args, fn: fn())

    result = server._mcp_run_sql_multi(
        {
            "sheet_join": "SELECT * FROM selected_sheet",
            "warehouse_only": "SELECT * FROM public.accounts",
        },
        limit=25,
        ctx="active-context",
    )

    assert result["results"]["sheet_join"]["sheet"]["surface_id"] == surface_id
    assert result["results"]["warehouse_only"]["rows"] == [{"ordinary": True}]
    assert observed["sheet"] == [(
        surface_id,
        "SELECT * FROM selected_sheet",
        {
            "as_of": None,
            "limit": 25,
            "read_mode": "snapshot",
            "default_view": "table",
            "ctx": "active-context",
        },
    )]
    assert observed["ordinary"] == [
        ("SELECT * FROM public.accounts", None, 25)
    ]


def test_catalog_lookup_notifies_each_mcp_session_once():
    class Session:
        def __init__(self):
            self.calls = 0

        async def send_tool_list_changed(self):
            self.calls += 1

    first = Session()
    second = Session()
    notified = set()

    assert asyncio.run(server._notify_tool_list_changed_once(
        SimpleNamespace(session=first), notified
    )) is True
    assert asyncio.run(server._notify_tool_list_changed_once(
        SimpleNamespace(session=first), notified
    )) is False
    assert asyncio.run(server._notify_tool_list_changed_once(
        SimpleNamespace(session=second), notified
    )) is True
    assert first.calls == 1
    assert second.calls == 1


def test_selected_private_sheet_query_reads_live_only_when_explicit(monkeypatch):
    surface_id = "f8db6009-66e0-4471-85b9-06e704334431"
    observed = {}
    selected = {
        "surface_id": surface_id,
        "title": "Pipeline Plan",
        "payload": {
            "columns": [{"name": "Owner", "type": "text", "sql_name": "owner"}],
            "rows": [{"Owner": "Old value"}],
        },
        "owner_email": "person@example.com",
        "provider_file_id": "sheet-file-id",
        "provider_sheet_id": 12,
        "spreadsheet_title": "Pipeline Plan",
        "sheet_name": "Current",
        "selected_range": "A1:A2",
        "first_row_header": True,
        "snapshot_hash": "b" * 64,
        "created_at": "2026-08-05T20:00:00Z",
    }

    async def inspect(conn_factory, owner, file_id, **options):
        observed.update(
            conn_factory=conn_factory,
            owner=owner,
            file_id=file_id,
            options=options,
        )
        return {
            "columns": [{"name": "Owner", "type": "text", "sql_name": "owner"}],
            "rows": [{"Owner": "Live value"}],
            "snapshot_hash": "c" * 64,
        }

    class Cursor:
        description = [SimpleNamespace(name="owner", type_code=25)]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement):
            observed["execution_sql"] = statement.as_string()

        def fetchmany(self, _count):
            return [{"owner": "Live value"}]

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def cursor():
            return Cursor()

    connection_factory = lambda **_kwargs: Connection()
    monkeypatch.setattr(server, "_selected_calliope_sheet_row", lambda *_args: selected)
    monkeypatch.setattr(calliope, "inspect_google_sheet", inspect)
    monkeypatch.setattr(server, "tool_validate_sql", lambda *_args: {
        "valid": True, "safe_select": True, "engine": "postgres", "rvbbit_tables": [],
    })
    monkeypatch.setattr(server, "_referenced_tables", lambda _sql: [])
    monkeypatch.setattr(server, "_session_pg_role", lambda: None)
    monkeypatch.setattr(server, "_conn", connection_factory)
    monkeypatch.setattr(server, "_logged", lambda _tool, _args, fn: fn())

    result = server._mcp_calliope_sheet_query(
        surface_id,
        "SELECT owner FROM selected_sheet",
        read_mode="live",
    )

    assert observed["conn_factory"] is connection_factory
    assert observed["owner"] == "person@example.com"
    assert observed["file_id"] == "sheet-file-id"
    assert observed["options"] == {
        "sheet_id": 12,
        "selected_range": "A1:A2",
        "first_row_header": True,
        "preview": False,
    }
    assert "Live value" in observed["execution_sql"]
    assert "Old value" not in observed["execution_sql"]
    assert result["sheet"]["read_mode"] == "live"
    assert result["sheet"]["snapshot_hash"] == "c" * 64
    assert result["sheet"]["source_snapshot_hash"] == "b" * 64
    assert result["sheet"]["frozen_snapshot"] is False
    assert result["lineage"]["sheet_read_mode"] == "live"


def test_real_mcp_context_unlocks_only_the_selected_private_document(monkeypatch):
    observed = {}

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _query, params):
            return SimpleNamespace(
                fetchone=lambda: (
                    {"owner_email": "person@example.com"}
                    if params == ("calliope_private_turn", 102892)
                    else None
                )
            )

    monkeypatch.setattr(
        server,
        "_authenticated_caller",
        lambda: ("calliope@example.com", "static-key"),
    )
    monkeypatch.setattr(server, "_conn", Connection)
    monkeypatch.setattr(server, "_record", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        server,
        "tool_brain_get_doc",
        lambda doc_id, caller: observed.update(doc_id=doc_id, caller=caller)
        or {"doc_id": doc_id},
    )

    async def exercise():
        mcp = FastMCP("private-document-scope")
        server._register(mcp)
        metadata = {
            server._HERMES_CALLER_META_KEY: {
                "source": "hermes",
                "platform": "api_server",
                "session_id": "calliope_private_turn",
            }
        }
        async with create_connected_server_and_client_session(mcp._mcp_server) as client:
            params = mcp_types.CallToolRequestParams(
                name="brain_get_doc",
                arguments={"doc_id": 102892},
                **{"_meta": metadata},
            )
            request = mcp_types.ClientRequest(
                mcp_types.CallToolRequest(method="tools/call", params=params)
            )
            result = await client.send_request(request, mcp_types.CallToolResult)
            assert result.isError is False

    asyncio.run(exercise())

    assert observed == {"doc_id": 102892, "caller": "person@example.com"}


def test_non_gchat_or_non_email_metadata_is_ignored(monkeypatch):
    monkeypatch.setattr(server, "_authenticated_caller", lambda: ("calliope@example.com", "static-key"))

    assert server._forwarded_mcp_caller(_context("not-an-email")) is None
    assert server._forwarded_mcp_caller(
        _context("person@example.com", platform="slack")
    ) is None


def test_real_mcp_meta_shape_is_read_and_context_stays_out_of_tool_schemas(monkeypatch):
    metadata = {
        server._HERMES_CALLER_META_KEY: {
            "source": "hermes",
            "platform": "google_chat",
            "user_id": "person@example.com",
        }
    }
    params = mcp_types.CallToolRequestParams(
        name="publish_dashboard", arguments={"name": "Daily"}, **{"_meta": metadata}
    )
    context = SimpleNamespace(request_context=SimpleNamespace(
        meta=params.meta,
        request=mcp_types.ClientRequest(
            mcp_types.CallToolRequest(method="tools/call", params=params)
        ),
    ))
    monkeypatch.setattr(server, "_authenticated_caller", lambda: ("calliope@example.com", "static-key"))

    assert server._forwarded_mcp_caller(context) == "person@example.com"

    mcp = FastMCP("attribution-schema")
    server._register(mcp)
    tools = asyncio.run(mcp.list_tools())
    tool_index = {tool.name: tool for tool in tools}
    schemas = {
        tool.name: set((tool.inputSchema.get("properties") or {}).keys())
        for tool in tools
    }
    for name in (
        "upload_artifact", "publish_dashboard", "update_dashboard",
        "create_live_app", "update_live_app",
        "brain_get_doc", "brain_context", "brain_related", "calliope_sheet_snapshot",
        "calliope_sheet_query", "run_sql", "run_sql_multi", "search_tools",
        "get_tool_help",
    ):
        assert "ctx" not in schemas[name]
    assert "read_mode" in schemas["calliope_sheet_query"]
    for name in ("calliope_sheet_query", "run_sql", "run_sql_multi"):
        view_schema = tool_index[name].inputSchema["properties"]["default_view"]
        assert view_schema["default"] == "table"
        assert view_schema["enum"] == ["table", "chart"]


def test_request_metadata_attributes_an_ordinary_tool_without_injected_context(monkeypatch):
    """The request envelope applies to every MCP tool, not only publishers."""
    observed = {}
    monkeypatch.setattr(
        server, "_authenticated_caller",
        lambda: ("calliope@example.com", "static-key"),
    )
    monkeypatch.setattr(
        server, "tool_search_data",
        lambda *_args, **_kwargs: {"matches": []},
    )
    monkeypatch.setattr(
        server, "_record",
        lambda tool, *_args, **_kwargs: observed.update(
            tool=tool,
            caller=server._caller(),
            client=server._mcp_client_implementation(),
        ),
    )

    async def exercise():
        mcp = FastMCP("all-tool-attribution")
        server._register(mcp)
        metadata = {
            server._HERMES_CALLER_META_KEY: {
                "source": "hermes",
                "platform": "google_chat",
                "user_id": "Person@Example.com",
            }
        }
        client_info = mcp_types.Implementation(
            name="hermes-agent", title="Hermes Agent", version="0.19.0"
        )
        async with create_connected_server_and_client_session(
            mcp._mcp_server, client_info=client_info
        ) as client:
            params = mcp_types.CallToolRequestParams(
                name="search_data",
                arguments={"query": "revenue"},
                **{"_meta": metadata},
            )
            request = mcp_types.ClientRequest(
                mcp_types.CallToolRequest(method="tools/call", params=params)
            )
            result = await client.send_request(request, mcp_types.CallToolResult)
            assert result.isError is False

    asyncio.run(exercise())

    assert observed == {
        "tool": "search_data",
        "caller": ("person@example.com", "static-key"),
        "client": {
            "name": "hermes-agent",
            "title": "Hermes Agent",
            "version": "0.19.0",
        },
    }
    assert server._FORWARDED_CALLER.get() is None
