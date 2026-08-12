FROM python:3.12-slim
WORKDIR /app
COPY services/warehouse-mcp/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -c "from mcp.server.fastmcp import FastMCP"
RUN python -m playwright install --with-deps chromium
COPY services/warehouse-mcp/server.py services/warehouse-mcp/auth.py services/warehouse-mcp/calliope.py services/warehouse-mcp/calliope_pages.py services/warehouse-mcp/calliope_dreams.py services/warehouse-mcp/calliope_access.py services/warehouse-mcp/application_teams.py services/warehouse-mcp/artifact_access.py services/warehouse-mcp/playbook_access.py services/warehouse-mcp/business_topology_jobs.py services/warehouse-mcp/warehouse_theme.py services/warehouse-mcp/dashboard_template.html ./
COPY services/warehouse-mcp/backgrounds ./backgrounds
COPY services/warehouse-mcp/calliope ./calliope
COPY services/warehouse-mcp/theme ./theme
COPY services/warehouse-mcp/charts ./charts
COPY services/warehouse-mcp/examples ./examples
COPY bench/business_topology ./business_topology
EXPOSE 8765
# Remote streamable-HTTP MCP + shared-key auth (set WAREHOUSE_MCP_KEY + WAREHOUSE_DSN).
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('WAREHOUSE_MCP_PORT','8765')+'/health').read()" || exit 1
CMD ["python", "server.py", "--http"]
