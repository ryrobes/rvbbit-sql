"""MCP integration — Phase 1.

Deterministic tests against the 'rvbbit-test' stdio MCP server baked into
the mcp-gateway image (it exposes echo/add/failing). Live tests against
the GitHub MCP server are gated behind RUN_MCP_LIVE=1 (uses GITHUB_TOKEN
from the gateway's env).
"""
import json
import os
import uuid

import pytest

LIVE = pytest.mark.skipif(
    os.environ.get("RUN_MCP_LIVE") != "1",
    reason="set RUN_MCP_LIVE=1 to run (uses GITHUB_TOKEN from gateway env)",
)

LIVE_FULL = pytest.mark.skipif(
    os.environ.get("RUN_MCP_LIVE") != "1" or os.environ.get("RUN_LLM_TESTS") != "1",
    reason="set RUN_MCP_LIVE=1 + RUN_LLM_TESTS=1 (real GitHub + OpenRouter)",
)


@pytest.fixture
def test_server(rvbbit):
    """Register the deterministic stdio test MCP server."""
    name = f"mcptest_{uuid.uuid4().hex[:6]}"
    rvbbit.execute(
        "SELECT rvbbit.register_mcp_server("
        "  server_name => %s, server_transport => 'stdio', "
        "  server_command => 'python', "
        "  server_args => ARRAY['/opt/mcp-test-server/main.py'])",
        (name,),
    )
    n = rvbbit.execute("SELECT rvbbit.refresh_mcp_server(%s)", (name,)).fetchone()[0]
    assert n >= 3, f"expected >= 3 tools, got {n}"
    yield name
    rvbbit.execute("SELECT rvbbit.drop_mcp_server(%s)", (name,))


# ---- catalog -------------------------------------------------------------


def test_gateway_runtime_registered_ready(rvbbit):
    row = rvbbit.execute(
        """
        SELECT name, endpoint_url, status
        FROM rvbbit.mcp_gateways
        WHERE status = 'ready'
        ORDER BY (name = 'mcp_default') DESC, updated_at DESC
        LIMIT 1
        """
    ).fetchone()
    assert row is not None, "expected a ready MCP Gateway runtime"
    configured = rvbbit.execute("SELECT rvbbit.mcp_gateway_endpoint()").fetchone()[0]
    assert configured.rstrip("/") == row[1].rstrip("/")


def test_server_registered(rvbbit, test_server):
    row = rvbbit.execute(
        "SELECT transport, command FROM rvbbit.mcp_servers WHERE name = %s",
        (test_server,),
    ).fetchone()
    assert row == ("stdio", "python")


def test_tools_discovered(rvbbit, test_server):
    names = {
        r[0]
        for r in rvbbit.execute(
            "SELECT name FROM rvbbit.mcp_tools WHERE server = %s ORDER BY name",
            (test_server,),
        ).fetchall()
    }
    assert {"echo", "add", "failing"}.issubset(names), names


def test_refresh_replaces_rows(rvbbit, test_server):
    # Refresh twice — counts should be the same (no duplicate rows).
    n1 = rvbbit.execute("SELECT rvbbit.refresh_mcp_server(%s)", (test_server,)).fetchone()[0]
    rows = rvbbit.execute(
        "SELECT count(*) FROM rvbbit.mcp_tools WHERE server = %s",
        (test_server,),
    ).fetchone()[0]
    assert rows == n1


def test_drop_cascades_tools(rvbbit):
    """drop_mcp_server cascades to mcp_tools but spares mcp_invocations."""
    name = f"drop_{uuid.uuid4().hex[:6]}"
    rvbbit.execute(
        "SELECT rvbbit.register_mcp_server("
        "  server_name => %s, server_transport => 'stdio', "
        "  server_command => 'python', "
        "  server_args => ARRAY['/opt/mcp-test-server/main.py'])",
        (name,),
    )
    rvbbit.execute("SELECT rvbbit.refresh_mcp_server(%s)", (name,))
    rvbbit.execute(
        "SELECT rvbbit.mcp_call(%s, 'echo', '{\"text\":\"x\"}'::jsonb)", (name,)
    )
    rvbbit.execute("SELECT rvbbit.drop_mcp_server(%s)", (name,))
    assert rvbbit.execute(
        "SELECT count(*) FROM rvbbit.mcp_tools WHERE server = %s", (name,)
    ).fetchone()[0] == 0
    # invocations preserved
    assert rvbbit.execute(
        "SELECT count(*) FROM rvbbit.mcp_invocations WHERE server = %s", (name,)
    ).fetchone()[0] >= 1


# ---- dispatch ------------------------------------------------------------


def test_call_echo(rvbbit, test_server):
    out = rvbbit.execute(
        "SELECT rvbbit.mcp_call(%s, 'echo', '{\"text\":\"hello\"}'::jsonb)",
        (test_server,),
    ).fetchone()[0]
    assert out["isError"] is False
    text = next(b["text"] for b in out["content"] if b.get("type") == "text")
    assert text == "hello"


def test_call_add(rvbbit, test_server):
    out = rvbbit.execute(
        "SELECT rvbbit.mcp_call(%s, 'add', '{\"a\":2,\"b\":3}'::jsonb)",
        (test_server,),
    ).fetchone()[0]
    text = next(b["text"] for b in out["content"] if b.get("type") == "text")
    assert "5" in text


def test_mcp_text_helper(rvbbit, test_server):
    text = rvbbit.execute(
        "SELECT rvbbit.mcp_text("
        "  rvbbit.mcp_call(%s, 'echo', '{\"text\":\"world\"}'::jsonb))",
        (test_server,),
    ).fetchone()[0]
    assert text == "world"


def test_tool_error_recorded(rvbbit, test_server):
    out = rvbbit.execute(
        "SELECT rvbbit.mcp_call(%s, 'failing', '{}'::jsonb)",
        (test_server,),
    ).fetchone()[0]
    assert out["isError"] is True
    err = rvbbit.execute(
        "SELECT error FROM rvbbit.mcp_invocations "
        "WHERE server = %s AND tool = 'failing' "
        "ORDER BY invocation_at DESC LIMIT 1",
        (test_server,),
    ).fetchone()[0]
    assert err and ("deliberate" in err.lower() or "fail" in err.lower())


def test_invocations_logged(rvbbit, test_server):
    query_id = rvbbit.execute("SELECT rvbbit.reset_query_id()").fetchone()[0]
    rvbbit.execute(
        "SELECT rvbbit.mcp_call(%s, 'echo', '{\"text\":\"audit-me\"}'::jsonb)",
        (test_server,),
    )
    row = rvbbit.execute(
        "SELECT tool, args, error, latency_ms, query_id FROM rvbbit.mcp_invocations "
        "WHERE server = %s AND tool = 'echo' "
        "ORDER BY invocation_at DESC LIMIT 1",
        (test_server,),
    ).fetchone()
    assert row[0] == "echo"
    assert row[1] == {"text": "audit-me"}
    assert row[2] is None
    assert row[3] is not None and row[3] >= 0
    assert str(row[4]) == str(query_id)


def test_unknown_server_errors(rvbbit):
    with pytest.raises(Exception):
        rvbbit.execute(
            "SELECT rvbbit.mcp_call('no_such_server_zzz', 'echo', '{}'::jsonb)"
        )


# ---- live (real GitHub MCP server via npx) -------------------------------


# ---- Phase 2: mcp_rows (SETOF jsonb relational surface) ------------------


def test_mcp_rows_top_level_array(rvbbit, test_server):
    """list_items returns a JSON array — one row per element."""
    rows = rvbbit.execute(
        f"SELECT row->>'name' FROM rvbbit.mcp_rows("
        f"  '{test_server}', 'list_items', '{{\"n\":4}}'::jsonb) AS row "
        "ORDER BY row->>'name'"
    ).fetchall()
    assert [r[0] for r in rows] == ["item0", "item1", "item2", "item3"]


def test_mcp_rows_nested_items_key(rvbbit, test_server):
    """search returns {query, total, items:[…]} — mcp_rows finds 'items'."""
    rows = rvbbit.execute(
        f"SELECT row->>'name' FROM rvbbit.mcp_rows("
        f"  '{test_server}', 'search', '{{\"q\":\"foo\"}}'::jsonb) AS row "
        "ORDER BY row->>'name'"
    ).fetchall()
    assert [r[0] for r in rows] == ["foo-a", "foo-b", "foo-c"]


def test_mcp_rows_plain_text_single_row(rvbbit, test_server):
    """echo returns a plain string — one row containing that text."""
    rows = rvbbit.execute(
        f"SELECT row FROM rvbbit.mcp_rows("
        f"  '{test_server}', 'echo', '{{\"text\":\"hi\"}}'::jsonb) AS row"
    ).fetchall()
    assert len(rows) == 1
    # echo's return is the bare string "hi" — surfaces as Value::String
    assert rows[0][0] in ("hi", '"hi"')


# ---- Phase 2: mcp as an operator node kind -------------------------------


def test_mcp_node_in_operator(rvbbit, test_server):
    """An operator with a single mcp node passes inputs through and returns
    the tool's output (parsed as JSON when possible)."""
    op = f"mcpop_{uuid.uuid4().hex[:6]}"
    steps = [
        {
            "name": "fetch",
            "kind": "mcp",
            "server": test_server,
            "tool": "search",
            "inputs": {"q": "{{ inputs.topic }}"},
        }
    ]
    rvbbit.execute(
        "SELECT rvbbit.create_operator("
        "  op_name => %s, op_arg_names => ARRAY['topic'], "
        "  op_return_type => 'jsonb', op_steps => %s::jsonb)",
        (op, json.dumps(steps)),
    )
    try:
        out = rvbbit.execute(f"SELECT rvbbit.{op}('alpha')").fetchone()[0]
        # output is the search envelope, parsed
        assert out["query"] == "alpha"
        assert out["total"] == 3
        assert {i["name"] for i in out["items"]} == {"alpha-a", "alpha-b", "alpha-c"}
        # sub_calls audit shows the mcp node
        kind, model = rvbbit.execute(
            "SELECT sub_calls->0->>'kind', sub_calls->0->>'model' "
            "FROM rvbbit.receipts WHERE operator = %s "
            "ORDER BY invocation_at DESC LIMIT 1",
            (op,),
        ).fetchone()
        assert kind == "mcp"
        assert model == f"{test_server}.search"
    finally:
        rvbbit.execute(f"DELETE FROM rvbbit.operators WHERE name = '{op}'")
        rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit.{op}(text, jsonb)")


def test_mcp_node_chains_with_code(rvbbit, test_server):
    """mcp → code: an mcp node's parsed-JSON output feeds a code step."""
    op = f"mcpcode_{uuid.uuid4().hex[:6]}"
    steps = [
        {
            "name": "fetch",
            "kind": "mcp",
            "server": test_server,
            "tool": "echo",
            "inputs": {"text": "{{ inputs.x }}"},
        },
        {
            "name": "up",
            "kind": "code",
            "fn": "uppercase",
            "inputs": {"text": "{{ steps.fetch.output }}"},
        },
    ]
    rvbbit.execute(
        "SELECT rvbbit.create_operator("
        "  op_name => %s, op_arg_names => ARRAY['x'], "
        "  op_return_type => 'text', op_steps => %s::jsonb)",
        (op, json.dumps(steps)),
    )
    try:
        assert rvbbit.execute(f"SELECT rvbbit.{op}('hello')").fetchone()[0] == "HELLO"
    finally:
        rvbbit.execute(f"DELETE FROM rvbbit.operators WHERE name = '{op}'")
        rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit.{op}(text, jsonb)")


def test_mcp_node_tool_error_is_step_error(rvbbit, test_server):
    """A tool returning isError=true surfaces as a step error."""
    op = f"mcpfail_{uuid.uuid4().hex[:6]}"
    steps = [
        {
            "name": "boom",
            "kind": "mcp",
            "server": test_server,
            "tool": "failing",
            "inputs": {},
        }
    ]
    rvbbit.execute(
        "SELECT rvbbit.create_operator("
        "  op_name => %s, op_arg_names => ARRAY['x'], "
        "  op_return_type => 'text', op_steps => %s::jsonb)",
        (op, json.dumps(steps)),
    )
    try:
        # graceful failure — no value, sub_call.error captures the reason
        rvbbit.execute(f"SELECT rvbbit.{op}('ignored')")
        err = rvbbit.execute(
            "SELECT sub_calls->0->>'error' FROM rvbbit.receipts "
            "WHERE operator = %s ORDER BY invocation_at DESC LIMIT 1",
            (op,),
        ).fetchone()[0]
        assert err and ("deliberate" in err.lower() or "fail" in err.lower())
    finally:
        rvbbit.execute(f"DELETE FROM rvbbit.operators WHERE name = '{op}'")
        rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit.{op}(text, jsonb)")


# ---- live (real GitHub MCP server via npx) -------------------------------


@LIVE
def test_github_register_and_list(rvbbit):
    """Register the npm GitHub MCP server (via npx) and verify discovery."""
    name = f"gh_{uuid.uuid4().hex[:6]}"
    rvbbit.execute(
        "SELECT rvbbit.register_mcp_server("
        "  server_name => %s, server_transport => 'stdio', "
        "  server_command => 'npx', "
        "  server_args => ARRAY['-y', '@modelcontextprotocol/server-github'], "
        "  server_env => %s::jsonb, "
        "  server_timeout_ms => 60000)",
        (name, json.dumps({"GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"})),
    )
    try:
        n = rvbbit.execute(
            "SELECT rvbbit.refresh_mcp_server(%s)", (name,)
        ).fetchone()[0]
        assert n >= 5, f"github MCP should expose many tools, got {n}"
        names = {
            r[0]
            for r in rvbbit.execute(
                "SELECT name FROM rvbbit.mcp_tools WHERE server = %s", (name,)
            ).fetchall()
        }
        # the exact tool names drift; just confirm something issue/repo-shaped exists
        assert any("repo" in n.lower() or "issue" in n.lower() for n in names), names
    finally:
        rvbbit.execute("SELECT rvbbit.drop_mcp_server(%s)", (name,))


@LIVE
def test_github_mcp_node_in_operator(rvbbit):
    """A mcp-node operator hits the real GitHub MCP server end-to-end."""
    server = f"gh_{uuid.uuid4().hex[:6]}"
    op = f"repo_{uuid.uuid4().hex[:6]}"
    rvbbit.execute(
        "SELECT rvbbit.register_mcp_server("
        "  server_name => %s, server_transport => 'stdio', "
        "  server_command => 'npx', "
        "  server_args => ARRAY['-y', '@modelcontextprotocol/server-github'], "
        "  server_env => %s::jsonb, "
        "  server_timeout_ms => 60000)",
        (server, json.dumps({"GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"})),
    )
    try:
        steps = [
            {
                "name": "fetch",
                "kind": "mcp",
                "server": server,
                "tool": "search_repositories",
                "inputs": {"query": "{{ inputs.q }}", "perPage": 3},
            }
        ]
        rvbbit.execute(
            "SELECT rvbbit.create_operator("
            "  op_name => %s, op_arg_names => ARRAY['q'], "
            "  op_return_type => 'jsonb', op_steps => %s::jsonb)",
            (op, json.dumps(steps)),
        )
        try:
            out = rvbbit.execute(
                f"SELECT rvbbit.{op}('anthropic-ai/claude-code')"
            ).fetchone()[0]
            assert out["total_count"] > 0
            assert len(out["items"]) > 0
        finally:
            rvbbit.execute(f"DELETE FROM rvbbit.operators WHERE name = '{op}'")
            rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit.{op}(text, jsonb)")
    finally:
        rvbbit.execute("SELECT rvbbit.drop_mcp_server(%s)", (server,))


@LIVE_FULL
def test_github_mcp_then_llm_chained(rvbbit):
    """The killer demo: github.search_repositories → llm.summarize.
    Real MCP fetch chained with a real OpenRouter LLM call in one operator."""
    server = f"gh_{uuid.uuid4().hex[:6]}"
    op = f"sum_{uuid.uuid4().hex[:6]}"
    rvbbit.execute(
        "SELECT rvbbit.register_mcp_server("
        "  server_name => %s, server_transport => 'stdio', "
        "  server_command => 'npx', "
        "  server_args => ARRAY['-y', '@modelcontextprotocol/server-github'], "
        "  server_env => %s::jsonb, "
        "  server_timeout_ms => 60000)",
        (server, json.dumps({"GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"})),
    )
    try:
        steps = [
            {
                "name": "fetch",
                "kind": "mcp",
                "server": server,
                "tool": "search_repositories",
                "inputs": {"query": "{{ inputs.q }}", "perPage": 1},
            },
            {
                "name": "summarize",
                "kind": "llm",
                "model": "openai/gpt-5.4-mini",
                "system": "Summarize what the repo does in ONE concise sentence. No preamble.",
                "user": "Repo: {{ steps.fetch.output.items.0.full_name }}\n"
                        "Description: {{ steps.fetch.output.items.0.description }}",
            },
        ]
        rvbbit.execute(
            "SELECT rvbbit.create_operator("
            "  op_name => %s, op_arg_names => ARRAY['q'], "
            "  op_return_type => 'text', op_steps => %s::jsonb)",
            (op, json.dumps(steps)),
        )
        try:
            out = rvbbit.execute(
                f"SELECT rvbbit.{op}('anthropic-ai/claude-code')"
            ).fetchone()[0]
            assert out and len(out) > 20  # got a real summary
            # the pipeline audit should show mcp -> llm
            kinds = rvbbit.execute(
                "SELECT sub_calls->0->>'kind', sub_calls->1->>'kind' "
                "FROM rvbbit.receipts WHERE operator = %s "
                "ORDER BY invocation_at DESC LIMIT 1",
                (op,),
            ).fetchone()
            assert kinds == ("mcp", "llm")
        finally:
            rvbbit.execute(f"DELETE FROM rvbbit.operators WHERE name = '{op}'")
            rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit.{op}(text, jsonb)")
    finally:
        rvbbit.execute("SELECT rvbbit.drop_mcp_server(%s)", (server,))


# ---- Phase 3: typed wrappers + observability views ----------------------


def test_generate_wrappers(rvbbit, test_server):
    """generate_mcp_wrappers creates a per-tool SETOF jsonb function in a
    per-server schema. Idempotent."""
    n = rvbbit.execute(
        "SELECT rvbbit.generate_mcp_wrappers(%s)", (test_server,)
    ).fetchone()[0]
    assert n == 5  # echo, add, failing, list_items, search

    fns = {
        r[0]
        for r in rvbbit.execute(
            "SELECT proname FROM pg_proc p JOIN pg_namespace n "
            "ON p.pronamespace = n.oid WHERE n.nspname = %s",
            (test_server,),
        ).fetchall()
    }
    assert {"echo", "add", "failing", "list_items", "search"}.issubset(fns), fns

    # Re-run → same count, no duplicate-object errors (idempotent).
    assert (
        rvbbit.execute(
            "SELECT rvbbit.generate_mcp_wrappers(%s)", (test_server,)
        ).fetchone()[0]
        == 5
    )


def test_wrapper_call_string_arg(rvbbit, test_server):
    rvbbit.execute("SELECT rvbbit.generate_mcp_wrappers(%s)", (test_server,))
    row = rvbbit.execute(
        f"SELECT r FROM {test_server}.echo(text => 'wrapped') r"
    ).fetchone()
    # echo returns the bare string; mcp_rows surfaces it as one row.
    assert row[0] in ("wrapped", '"wrapped"')


def test_wrapper_call_numeric_args(rvbbit, test_server):
    rvbbit.execute("SELECT rvbbit.generate_mcp_wrappers(%s)", (test_server,))
    row = rvbbit.execute(
        f"SELECT r FROM {test_server}.add(a => 7, b => 5) r"
    ).fetchone()
    # Result is "12" or 12 depending on serialization.
    s = str(row[0])
    assert "12" in s


def test_wrapper_setof_unwraps_list(rvbbit, test_server):
    """list_items's multi-block FastMCP return → multiple rows from the wrapper."""
    rvbbit.execute("SELECT rvbbit.generate_mcp_wrappers(%s)", (test_server,))
    names = [
        r[0]
        for r in rvbbit.execute(
            f"SELECT r->>'name' FROM {test_server}.list_items(n => 4) r "
            "ORDER BY r->>'name'"
        ).fetchall()
    ]
    assert names == ["item0", "item1", "item2", "item3"]


def test_wrapper_setof_unwraps_nested(rvbbit, test_server):
    rvbbit.execute("SELECT rvbbit.generate_mcp_wrappers(%s)", (test_server,))
    names = [
        r[0]
        for r in rvbbit.execute(
            f"SELECT r->>'name' FROM {test_server}.search(q => 'x') r "
            "ORDER BY r->>'name'"
        ).fetchall()
    ]
    assert names == ["x-a", "x-b", "x-c"]


def test_wrapper_omits_unset_args(rvbbit, test_server):
    """An optional arg left as default (NULL) is NOT forwarded to the tool —
    the tool sees only the args the caller actually set."""
    rvbbit.execute("SELECT rvbbit.generate_mcp_wrappers(%s)", (test_server,))
    # echo's `text` is required, but pass it as a kwarg. The point: no
    # extra args sneak in (would error if echo received an unknown key).
    row = rvbbit.execute(
        f"SELECT r FROM {test_server}.echo(text => 'lean') r"
    ).fetchone()
    assert row[0] in ("lean", '"lean"')


def test_mcp_usage_view(rvbbit, test_server):
    """mcp_usage rolls up per-(server, tool) call stats."""
    rvbbit.execute(
        "SELECT rvbbit.mcp_call(%s, 'echo', '{\"text\":\"u1\"}'::jsonb)",
        (test_server,),
    )
    rvbbit.execute(
        "SELECT rvbbit.mcp_call(%s, 'echo', '{\"text\":\"u2\"}'::jsonb)",
        (test_server,),
    )
    row = rvbbit.execute(
        "SELECT n_calls, n_errors FROM rvbbit.mcp_usage "
        "WHERE server = %s AND tool = 'echo'",
        (test_server,),
    ).fetchone()
    assert row[0] >= 2
    assert row[1] == 0


def test_mcp_health_view(rvbbit, test_server):
    """mcp_health reports per-server config + discovery + call snapshot."""
    rvbbit.execute(
        "SELECT rvbbit.mcp_call(%s, 'echo', '{\"text\":\"hb\"}'::jsonb)",
        (test_server,),
    )
    row = rvbbit.execute(
        "SELECT transport, n_tools, last_discovered_at IS NOT NULL, "
        "       last_call_at IS NOT NULL "
        "FROM rvbbit.mcp_health WHERE name = %s",
        (test_server,),
    ).fetchone()
    assert row == ("stdio", 5, True, True)


# ---- Phase 4: resources, selective caching, active probe ----------------


def test_resources_discovered(rvbbit, test_server):
    """Resources land in rvbbit.mcp_resources on refresh."""
    rows = rvbbit.execute(
        "SELECT uri FROM rvbbit.mcp_resources WHERE server = %s ORDER BY uri",
        (test_server,),
    ).fetchall()
    uris = {r[0] for r in rows}
    assert {"rvbbit-test://hello", "rvbbit-test://config"}.issubset(uris), uris


def test_mcp_resource_envelope(rvbbit, test_server):
    """mcp_resource returns the full read envelope."""
    out = rvbbit.execute(
        "SELECT rvbbit.mcp_resource(%s, 'rvbbit-test://hello')",
        (test_server,),
    ).fetchone()[0]
    assert "contents" in out
    text = next(
        c.get("text") for c in out["contents"] if c.get("text") is not None
    )
    assert text == "hello from the test server"


def test_mcp_resource_text_helper(rvbbit, test_server):
    text = rvbbit.execute(
        "SELECT rvbbit.mcp_resource_text(%s, 'rvbbit-test://config')",
        (test_server,),
    ).fetchone()[0]
    assert text == '{"key":"value","count":42}'


# ---- caching ------------------------------------------------------------


def test_caching_hit_then_miss(rvbbit, test_server):
    """Mark echo cacheable; same args → cache hit; different args → miss."""
    rvbbit.execute(
        "SELECT rvbbit.set_mcp_tool_caching(%s, 'echo')", (test_server,)
    )
    rvbbit.execute("SELECT rvbbit.purge_mcp_cache(%s, 'echo')", (test_server,))

    # First call: miss
    rvbbit.execute(
        "SELECT rvbbit.mcp_call(%s, 'echo', '{\"text\":\"cached\"}'::jsonb)",
        (test_server,),
    )
    # Second identical call: hit
    rvbbit.execute(
        "SELECT rvbbit.mcp_call(%s, 'echo', '{\"text\":\"cached\"}'::jsonb)",
        (test_server,),
    )
    # Different args: miss
    rvbbit.execute(
        "SELECT rvbbit.mcp_call(%s, 'echo', '{\"text\":\"different\"}'::jsonb)",
        (test_server,),
    )

    hits = [
        r[0]
        for r in rvbbit.execute(
            "SELECT cache_hit FROM rvbbit.mcp_invocations "
            "WHERE server = %s AND tool = 'echo' "
            "  AND (args->>'text' IN ('cached', 'different')) "
            "ORDER BY invocation_at",
            (test_server,),
        ).fetchall()
    ]
    # First 'cached' miss, second 'cached' hit, 'different' miss
    assert hits[-3:] == [False, True, False], hits


def test_caching_keyed_canonically(rvbbit, test_server):
    """Args differing only in key order hash the same — same cache entry."""
    rvbbit.execute(
        "SELECT rvbbit.set_mcp_tool_caching(%s, 'add')", (test_server,)
    )
    rvbbit.execute("SELECT rvbbit.purge_mcp_cache(%s, 'add')", (test_server,))

    rvbbit.execute(
        "SELECT rvbbit.mcp_call(%s, 'add', '{\"a\":1,\"b\":2}'::jsonb)",
        (test_server,),
    )
    # Same args, JSON key order swapped → should be a cache hit
    rvbbit.execute(
        "SELECT rvbbit.mcp_call(%s, 'add', '{\"b\":2,\"a\":1}'::jsonb)",
        (test_server,),
    )
    hit = rvbbit.execute(
        "SELECT cache_hit FROM rvbbit.mcp_invocations "
        "WHERE server = %s AND tool = 'add' "
        "ORDER BY invocation_at DESC LIMIT 1",
        (test_server,),
    ).fetchone()[0]
    assert hit is True


def test_caching_skips_errors(rvbbit, test_server):
    """A tool returning isError=true is NOT cached — failures don't poison."""
    rvbbit.execute(
        "SELECT rvbbit.set_mcp_tool_caching(%s, 'failing')", (test_server,)
    )
    rvbbit.execute("SELECT rvbbit.purge_mcp_cache(%s, 'failing')", (test_server,))

    # Two calls, both error
    rvbbit.execute(
        "SELECT rvbbit.mcp_call(%s, 'failing', '{}'::jsonb)", (test_server,)
    )
    rvbbit.execute(
        "SELECT rvbbit.mcp_call(%s, 'failing', '{}'::jsonb)", (test_server,)
    )
    # No rows ended up in the cache
    n = rvbbit.execute(
        "SELECT count(*) FROM rvbbit.mcp_cache "
        "WHERE server = %s AND tool = 'failing'",
        (test_server,),
    ).fetchone()[0]
    assert n == 0
    # Both invocations are misses (cache_hit=false)
    hits = {
        r[0]
        for r in rvbbit.execute(
            "SELECT cache_hit FROM rvbbit.mcp_invocations "
            "WHERE server = %s AND tool = 'failing'",
            (test_server,),
        ).fetchall()
    }
    assert hits == {False}


def test_set_caching_unknown_tool_errors(rvbbit, test_server):
    with pytest.raises(Exception):
        rvbbit.execute(
            "SELECT rvbbit.set_mcp_tool_caching(%s, 'no_such_tool')",
            (test_server,),
        )


def test_refresh_preserves_caching(rvbbit, test_server):
    """A second refresh keeps the cacheable / ttl_seconds flags."""
    rvbbit.execute(
        "SELECT rvbbit.set_mcp_tool_caching(%s, 'echo', 300)", (test_server,)
    )
    rvbbit.execute("SELECT rvbbit.refresh_mcp_server(%s)", (test_server,))
    row = rvbbit.execute(
        "SELECT cacheable, ttl_seconds FROM rvbbit.mcp_tools "
        "WHERE server = %s AND name = 'echo'",
        (test_server,),
    ).fetchone()
    assert row == (True, 300)


def test_purge_returns_count(rvbbit, test_server):
    rvbbit.execute(
        "SELECT rvbbit.set_mcp_tool_caching(%s, 'echo')", (test_server,)
    )
    rvbbit.execute("SELECT rvbbit.purge_mcp_cache(%s, 'echo')", (test_server,))
    rvbbit.execute(
        "SELECT rvbbit.mcp_call(%s, 'echo', '{\"text\":\"purge1\"}'::jsonb)",
        (test_server,),
    )
    rvbbit.execute(
        "SELECT rvbbit.mcp_call(%s, 'echo', '{\"text\":\"purge2\"}'::jsonb)",
        (test_server,),
    )
    n = rvbbit.execute(
        "SELECT rvbbit.purge_mcp_cache(%s, 'echo')", (test_server,)
    ).fetchone()[0]
    assert n >= 2


# ---- active probe -------------------------------------------------------


def test_mcp_probe_reachable(rvbbit, test_server):
    out = rvbbit.execute(
        "SELECT rvbbit.mcp_probe(%s)", (test_server,)
    ).fetchone()[0]
    assert out["reachable"] is True
    assert out["n_tools"] >= 5
    assert out["latency_ms"] >= 0
    assert out["error"] is None


def test_mcp_probe_unknown_server(rvbbit):
    """Probing a server that isn't registered returns reachable=false."""
    out = rvbbit.execute(
        "SELECT rvbbit.mcp_probe('no_such_server_zzz')"
    ).fetchone()[0]
    assert out["reachable"] is False
    assert out["error"] is not None


# ---- mcp_health updated --------------------------------------------------


def test_mcp_health_includes_resources(rvbbit, test_server):
    row = rvbbit.execute(
        "SELECT n_tools, n_resources FROM rvbbit.mcp_health WHERE name = %s",
        (test_server,),
    ).fetchone()
    assert row == (5, 2)
