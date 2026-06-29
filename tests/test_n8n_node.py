"""n8n operator node support.

The execution contract is a configured production webhook. The n8n database is
read only discovery metadata for Lens, so deterministic tests cover the SQL
registry and workflow enumeration without starting an n8n service.
"""

import json
import uuid


def test_register_n8n_runtime_roundtrip(rvbbit):
    name = f"n8n_{uuid.uuid4().hex[:8]}"
    try:
        row = rvbbit.execute(
            """
            SELECT rvbbit.register_n8n_runtime(
              %s, 'http://localhost:5678/', 'webhook/',
              'X-N8N-Token', 'N8N_API_TOKEN',
              '{"purpose":"test"}'::jsonb
            )
            """,
            (name,),
        ).fetchone()[0]
        assert row["name"] == name
        assert row["base_url"] == "http://localhost:5678"
        assert row["webhook_path_prefix"] == "/webhook"

        status = rvbbit.execute(
            """
            SELECT base_url, webhook_path_prefix, auth_configured, metadata
              FROM rvbbit.n8n_runtime_status()
             WHERE name = %s
            """,
            (name,),
        ).fetchone()
        assert status[0] == "http://localhost:5678"
        assert status[1] == "/webhook"
        assert status[2] is True
        assert status[3] == {"purpose": "test"}
    finally:
        rvbbit.execute("DELETE FROM rvbbit.n8n_runtimes WHERE name = %s", (name,))


def test_n8n_workflows_discovers_webhook_paths(rvbbit):
    schema = f"n8n_test_{uuid.uuid4().hex[:8]}"
    nodes = [
        {
            "id": "webhook-node",
            "name": "Lead webhook",
            "type": "n8n-nodes-base.webhook",
            "parameters": {
                "path": "lead-funnel",
                "httpMethod": "POST",
                "responseMode": "responseNode",
            },
        },
        {
            "id": "code-node",
            "name": "Normalize",
            "type": "n8n-nodes-base.code",
            "parameters": {},
        },
    ]
    try:
        rvbbit.execute(f"CREATE SCHEMA {schema}")
        rvbbit.execute(
            f"""
            CREATE TABLE {schema}.workflow_entity (
                id text PRIMARY KEY,
                name text NOT NULL,
                active boolean NOT NULL,
                nodes jsonb NOT NULL,
                "createdAt" timestamptz,
                "updatedAt" timestamptz
            )
            """
        )
        rvbbit.execute(
            f"""
            INSERT INTO {schema}.workflow_entity
              (id, name, active, nodes, "createdAt", "updatedAt")
            VALUES (%s, %s, true, %s::jsonb, now(), now())
            """,
            ("wf_1", "Lead Funnel", json.dumps(nodes)),
        )

        row = rvbbit.execute(
            """
            SELECT workflow_id, workflow_name, active, trigger_paths,
                   webhook_nodes, input_schema
              FROM rvbbit.n8n_workflows(%s)
            """,
            (schema,),
        ).fetchone()
        assert row[0] == "wf_1"
        assert row[1] == "Lead Funnel"
        assert row[2] is True
        assert row[3] == ["lead-funnel"]
        assert row[4][0]["node_name"] == "Lead webhook"
        assert row[4][0]["method"] == "POST"
        assert row[5]["additionalProperties"] is True
    finally:
        rvbbit.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
