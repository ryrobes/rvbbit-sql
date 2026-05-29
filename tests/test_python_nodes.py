"""Python nodes as rvbbit operator workflow primitives.

The catalog tests need only Postgres. The runtime tests need the managed
CPython sidecar:

  docker compose -f docker/docker-compose.yml \\
                 -f docker/docker-compose.sidecars.yml \\
                 up -d python-runtime
"""

from __future__ import annotations

import json
import os
import urllib.request
import uuid

import pytest


SIDECAR_BASE = os.environ.get(
    "RVBBIT_PYTHON_RUNTIME_BASE", "http://rvbbit-python-runtime:8080"
)
RUN_URL = os.environ.get("RVBBIT_PYTHON_RUNTIME_RUN_URL", f"{SIDECAR_BASE}/run")


def _alive() -> bool:
    try:
        urllib.request.urlopen(f"{SIDECAR_BASE}/health", timeout=3).read()
        return True
    except Exception:
        return False


PYTHON_RUNTIME = pytest.mark.skipif(
    not _alive(),
    reason=f"python-runtime sidecar not reachable at {SIDECAR_BASE}",
)


def _drop_python(rvbbit, handler: str | None = None, env: str | None = None) -> None:
    if handler:
        rvbbit.execute("DELETE FROM rvbbit.python_handlers WHERE name = %s", (handler,))
    if env:
        rvbbit.execute("DELETE FROM rvbbit.python_envs WHERE name = %s", (env,))
    rvbbit.execute("SELECT rvbbit.reload_python_runtime()")


def _drop_op(rvbbit, name: str, n_args: int) -> None:
    sig = ", ".join(["text"] * n_args + ["jsonb"])
    rvbbit.execute("DELETE FROM rvbbit.operators WHERE name = %s", (name,))
    rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit.{name}({sig})")


def test_python_env_and_handler_ddl(rvbbit):
    env = f"pyenv_{uuid.uuid4().hex[:8]}"
    handler = f"pyh_{uuid.uuid4().hex[:8]}"
    try:
        env_doc = rvbbit.execute(
            "SELECT rvbbit.create_python_env(%s, %s, %s::text[], NULL, %s)",
            (env, "3.12", [" packaging==24.2 ", ""], 1500),
        ).fetchone()[0]
        assert env_doc["name"] == env
        assert env_doc["requirements"] == ["packaging==24.2"]
        assert env_doc["timeout_ms"] == 1500
        assert len(env_doc["env_hash"]) == 32

        handler_doc = rvbbit.execute(
            "SELECT rvbbit.create_python_handler(%s, %s, %s, 'run', %s)",
            (
                handler,
                env,
                "def run(inputs):\n    return {'ok': True, 'rows': inputs.get('rows', 0)}\n",
                "catalog smoke",
            ),
        ).fetchone()[0]
        assert handler_doc["name"] == handler
        assert handler_doc["env_name"] == env
        assert len(handler_doc["code_hash"]) == 32
    finally:
        _drop_python(rvbbit, handler, env)


@PYTHON_RUNTIME
def test_python_node_sql_lookup_sla_scoring_with_ward(rvbbit):
    """A SQL lookup feeds Python scoring, then a post-ward validates shape.

    This is the DE/ops pattern: reference data stays in SQL; Python is the
    deterministic scoring glue inside the operator flow.
    """
    env = f"pyenv_{uuid.uuid4().hex[:8]}"
    handler = f"sla_score_{uuid.uuid4().hex[:8]}"
    op = f"ticket_sla_{uuid.uuid4().hex[:8]}"
    table = f"customer_dim_{uuid.uuid4().hex[:8]}"
    code = r'''
import re

def run(inputs):
    message = str(inputs.get("message") or "")
    tier = str(inputs.get("tier") or "standard").lower()
    revenue = float(inputs.get("annual_revenue") or 0)
    open_tickets = int(inputs.get("open_tickets") or 0)
    outage = re.search(r"\b(outage|down|cannot access|can't access|checkout)\b", message, re.I) is not None
    score = 0.0
    flags = []
    if tier in {"enterprise", "strategic"}:
        score += 0.35
        flags.append("high_value_account")
    if revenue >= 1000000:
        score += 0.25
        flags.append("revenue_risk")
    if open_tickets >= 3:
        score += 0.20
        flags.append("repeat_contact")
    if outage:
        score += 0.35
        flags.append("possible_outage")
    priority = "urgent" if score >= 0.70 else "elevated" if score >= 0.35 else "standard"
    return {
        "priority": priority,
        "score": round(min(score, 1.0), 3),
        "flags": flags,
        "normalized_message": " ".join(message.lower().split()),
    }
'''
    try:
        urllib.request.urlopen(f"{SIDECAR_BASE}/debug/reset", timeout=3, data=b"{}").read()
        rvbbit.execute(f"CREATE TABLE {table} (id int PRIMARY KEY, tier text, annual_revenue float8)")
        rvbbit.execute(
            f"INSERT INTO {table} VALUES (101, 'enterprise', 2400000), (202, 'standard', 12000)"
        )
        rvbbit.execute(
            "SELECT rvbbit.create_python_env(%s, '3.12', ARRAY[]::text[], %s, 2000)",
            (env, RUN_URL),
        )
        rvbbit.execute(
            "SELECT rvbbit.create_python_handler(%s, %s, %s)",
            (handler, env, code),
        )
        rvbbit.execute(
            "SELECT rvbbit.create_operator("
            "  op_name => %s, op_arg_names => ARRAY['customer_id','message','open_tickets'], "
            "  op_return_type => 'jsonb', op_steps => %s::jsonb)",
            (
                op,
                json.dumps(
                    [
                        {
                            "name": "cust",
                            "kind": "sql",
                            "sql": f"SELECT tier, annual_revenue FROM {table} WHERE id = $1::int",
                            "params": ["{{ inputs.customer_id }}"],
                        },
                        {
                            "name": "score",
                            "kind": "python",
                            "env": env,
                            "handler": handler,
                            "inputs": {
                                "message": "{{ inputs.message }}",
                                "open_tickets": "{{ inputs.open_tickets }}",
                                "tier": "{{ steps.cust.output.tier }}",
                                "annual_revenue": "{{ steps.cust.output.annual_revenue }}",
                            },
                        },
                    ]
                ),
            ),
        )
        rvbbit.execute(
            "SELECT rvbbit.set_operator_wards(%s, %s::jsonb)",
            (
                op,
                json.dumps(
                    {
                        "post": [
                            {
                                "validator": {
                                    "sql": "($output::jsonb ? 'priority') AND (($output::jsonb->>'priority') IN ('standard','elevated','urgent'))"
                                },
                                "mode": "blocking",
                            }
                        ]
                    }
                ),
            ),
        )
        rvbbit.execute("SELECT rvbbit.flush_cache()")
        rvbbit.execute("DELETE FROM rvbbit.receipts WHERE operator = %s", (op,))

        out = rvbbit.execute(
            f"SELECT rvbbit.{op}(%s, %s, %s)",
            ("101", "Checkout is down and our team cannot access invoices", "4"),
        ).fetchone()[0]
        assert out["priority"] == "urgent"
        assert "high_value_account" in out["flags"]
        assert "possible_outage" in out["flags"]

        kinds = rvbbit.execute(
            "SELECT jsonb_path_query_array(sub_calls, '$[*].kind') "
            "FROM rvbbit.receipts WHERE operator = %s "
            "ORDER BY invocation_at DESC LIMIT 1",
            (op,),
        ).fetchone()[0]
        assert kinds == ["sql", "python"]
    finally:
        rvbbit.execute(f"DROP TABLE IF EXISTS {table}")
        _drop_op(rvbbit, op, 3)
        _drop_python(rvbbit, handler, env)


@PYTHON_RUNTIME
def test_python_node_prewarm_event_stream_enrichment(rvbbit):
    """Python turns raw event stream fields into structured SQL outputs.

    The query shape is deliberately "Python in SQL", not SQL inside Python:
    the event stream is selected by SQL, prewarmed through the operator, and
    later row calls resolve from rvbbit's operator cache.
    """
    env = f"pyenv_{uuid.uuid4().hex[:8]}"
    handler = f"event_norm_{uuid.uuid4().hex[:8]}"
    op = f"event_enrich_{uuid.uuid4().hex[:8]}"
    table = f"web_events_{uuid.uuid4().hex[:8]}"
    code = r'''
from urllib.parse import urlparse

def run(inputs):
    url = str(inputs.get("url") or "")
    status = int(inputs.get("status") or 0)
    bytes_sent = int(inputs.get("bytes_sent") or 0)
    path = urlparse(url).path
    parts = [p for p in path.split("/") if p]
    product = parts[0] if parts else "root"
    status_class = f"{status // 100}xx" if status else "unknown"
    quality = "bad" if status >= 500 or bytes_sent < 0 else "ok"
    return {
        "product": product,
        "status_class": status_class,
        "mb": round(max(bytes_sent, 0) / 1048576.0, 3),
        "quality": quality,
    }
'''
    events = [
        ("https://app.example.com/billing/invoices/123", "200", "18432"),
        ("https://app.example.com/billing/payments", "502", "512"),
        ("https://app.example.com/search?q=logs", "200", "99123"),
        ("https://app.example.com/admin/users", "403", "1200"),
        ("https://app.example.com/billing/invoices/456", "200", "2048"),
        ("https://app.example.com/api/export", "504", "800"),
    ]
    try:
        urllib.request.urlopen(f"{SIDECAR_BASE}/debug/reset", timeout=3, data=b"{}").read()
        rvbbit.execute(f"CREATE TABLE {table} (url text, status text, bytes_sent text)")
        for row in events:
            rvbbit.execute(f"INSERT INTO {table} VALUES (%s, %s, %s)", row)
        rvbbit.execute(
            "SELECT rvbbit.create_python_env(%s, '3.12', ARRAY[]::text[], %s, 2000)",
            (env, RUN_URL),
        )
        rvbbit.execute("SELECT rvbbit.create_python_handler(%s, %s, %s)", (handler, env, code))
        rvbbit.execute(
            "SELECT rvbbit.create_operator("
            "  op_name => %s, op_arg_names => ARRAY['url','status','bytes_sent'], "
            "  op_return_type => 'jsonb', op_steps => %s::jsonb)",
            (
                op,
                json.dumps(
                    [
                        {
                            "name": "normalize",
                            "kind": "python",
                            "env": env,
                            "handler": handler,
                            "inputs": {
                                "url": "{{ inputs.url }}",
                                "status": "{{ inputs.status }}",
                                "bytes_sent": "{{ inputs.bytes_sent }}",
                            },
                        }
                    ]
                ),
            ),
        )
        rvbbit.execute("SELECT rvbbit.flush_cache()")
        rvbbit.execute("DELETE FROM rvbbit.receipts WHERE operator = %s", (op,))

        warm = rvbbit.execute(
            "SELECT * FROM rvbbit.prewarm_operator(%s, %s)",
            (op, f"SELECT url, status, bytes_sent FROM {table} ORDER BY url"),
        ).fetchone()
        assert warm[0] == len(events)
        assert warm[2] == len(events)
        assert warm[3] == 0
        before = rvbbit.execute(
            "SELECT count(*) FROM rvbbit.receipts WHERE operator = %s", (op,)
        ).fetchone()[0]

        rows = rvbbit.execute(
            f"SELECT rvbbit.{op}(url, status, bytes_sent) FROM {table} ORDER BY url"
        ).fetchall()
        outputs = [row[0] for row in rows]
        assert sum(1 for out in outputs if out["quality"] == "bad") == 2
        assert {out["product"] for out in outputs} >= {"billing", "admin", "api"}

        after = rvbbit.execute(
            "SELECT count(*) FROM rvbbit.receipts WHERE operator = %s", (op,)
        ).fetchone()[0]
        assert after == before, "post-prewarm SELECT should be served from cache"
        assert rvbbit.execute(
            "SELECT count(*) FROM rvbbit.receipts "
            "WHERE operator = %s AND sub_calls->0->>'kind' = 'python'",
            (op,),
        ).fetchone()[0] == len(events)
    finally:
        rvbbit.execute(f"DROP TABLE IF EXISTS {table}")
        _drop_op(rvbbit, op, 3)
        _drop_python(rvbbit, handler, env)
