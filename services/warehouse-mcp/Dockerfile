FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN python -m playwright install --with-deps chromium
COPY server.py auth.py dashboard_template.html ./
EXPOSE 8765
# Remote streamable-HTTP MCP + shared-key auth (set WAREHOUSE_MCP_KEY + WAREHOUSE_DSN).
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('WAREHOUSE_MCP_PORT','8765')+'/health').read()" || exit 1
CMD ["python", "server.py", "--http"]
