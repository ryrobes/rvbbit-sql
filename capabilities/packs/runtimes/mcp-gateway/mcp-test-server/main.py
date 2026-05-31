"""Tiny deterministic MCP server — used by rvbbit's MCP integration tests.

Exposes three trivial tools so we can prove dispatch + audit + error paths
end-to-end without network or LLM cost:

  echo(text: str) -> str       : returns text unchanged
  add(a: int, b: int) -> int   : returns a + b
  failing() -> str             : always raises (error-path test)

Runs as a stdio MCP server (the gateway's default transport).
"""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("rvbbit-test")


@mcp.tool()
def echo(text: str) -> str:
    """Echo the given text unchanged."""
    return text


@mcp.tool()
def add(a: int, b: int) -> int:
    """Return a + b."""
    return a + b


@mcp.tool()
def failing() -> str:
    """Always raises — used to exercise the error path."""
    raise RuntimeError("deliberate test failure")


@mcp.tool()
def list_items(n: int) -> list[dict]:
    """Return a list of n simple item dicts. Used to test mcp_rows on a
    top-level JSON array."""
    return [{"id": i, "name": f"item{i}"} for i in range(n)]


@mcp.tool()
def search(q: str) -> dict:
    """Return a result envelope {query, total, items:[…]}. Used to test
    mcp_rows' auto-extraction of nested array-bearing keys."""
    items = [{"name": f"{q}-a"}, {"name": f"{q}-b"}, {"name": f"{q}-c"}]
    return {"query": q, "total": len(items), "items": items}


# ---- Resources ------------------------------------------------------------
# Two static MCP resources for the Phase 4 read-by-URI surface tests.


@mcp.resource("rvbbit-test://hello")
def hello_resource() -> str:
    """A trivial text resource."""
    return "hello from the test server"


@mcp.resource("rvbbit-test://config")
def config_resource() -> str:
    """A resource whose text body is JSON."""
    return '{"key":"value","count":42}'


if __name__ == "__main__":
    mcp.run()
