# MCP gateway — hosts MCP server subprocesses (stdio + HTTP transports) and
# exposes them to the rvbbit pg extension over plain HTTP. PG backends never
# fork subprocesses themselves; they POST to this gateway, which manages
# subprocess lifecycle, per-server locking, and re-introspection.
#
# Node + npm are installed so `npx -y @modelcontextprotocol/server-…` works
# out of the box (most public MCP servers are npm packages).

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py /app/
COPY mcp-test-server /opt/mcp-test-server

# curl is kept for the healthcheck.
EXPOSE 9180
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
    CMD curl -fsS http://localhost:9180/health || exit 1
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9180", "--log-level", "info"]
