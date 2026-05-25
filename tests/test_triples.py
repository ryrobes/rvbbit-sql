"""First-class triples and KG ingestion primitives."""

import json
import uuid


def _kind(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _cleanup_kind(rvbbit, kind: str) -> None:
    rvbbit.execute("DELETE FROM rvbbit.kg_nodes WHERE kind = %s", (kind,))


def test_triples_seed_operator_exists(rvbbit):
    row = rvbbit.execute(
        """
        SELECT return_type, parser, retry->'until'->>'function'
        FROM rvbbit.operators
        WHERE name = 'triples'
        """
    ).fetchone()
    assert row == ("jsonb", "json", "rvbbit.triples_valid")


def test_triples_valid_accepts_strict_rows(rvbbit):
    raw = json.dumps(
        [
            {
                "subject_kind": "customer",
                "subject": "Acme Corp",
                "predicate": "reported",
                "object_kind": "issue",
                "object": "late shipment",
                "confidence": 0.92,
                "evidence": "Acme reported late shipments.",
                "properties": {"source": "ticket"},
            }
        ]
    )
    ok = rvbbit.execute("SELECT rvbbit.triples_valid(%s, '{}'::jsonb)", (raw,)).fetchone()[0]
    assert ok is True


def test_triples_valid_rejects_bad_shapes(rvbbit):
    bad = [
        "not json",
        "{}",
        '[{"subject":"Acme","predicate":"reported"}]',
        '[{"subject":"Acme","predicate":"reported","object":"x","extra":"bad"}]',
        '[{"subject":"Acme","predicate":"reported","object":"x","confidence":2}]',
        '[{"subject":"Acme","predicate":"reported","object":"x","properties":[]}]',
    ]
    for raw in bad:
        ok = rvbbit.execute("SELECT rvbbit.triples_valid(%s, '{}'::jsonb)", (raw,)).fetchone()[0]
        assert ok is False


def test_triples_json_rows_shapes_output(rvbbit):
    raw = json.dumps(
        [
            {
                "subject": "Acme Corp",
                "predicate": "reported",
                "object": "late shipment",
                "confidence": 1.4,
                "evidence": "Acme reported late shipments.",
                "properties": {"source": "ticket"},
                "extra_fact": "kept",
            },
            {"subject": "", "predicate": "ignored", "object": "missing subject"},
        ]
    )
    rows = rvbbit.execute(
        """
        SELECT subject_kind, subject, predicate, object_kind, object,
               confidence, evidence, properties
        FROM rvbbit.triples_json_rows(%s::jsonb)
        """,
        (raw,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0:7] == (
        "entity",
        "Acme Corp",
        "reported",
        "entity",
        "late shipment",
        1.0,
        "Acme reported late shipments.",
    )
    assert rows[0][7]["source"] == "ticket"
    assert rows[0][7]["extra_fact"] == "kept"


def test_kg_ingest_triples_from_query(rvbbit):
    customer_kind = _kind("tr_customer")
    issue_kind = _kind("tr_issue")
    query_id = uuid.uuid4()
    graph = f"tr_graph_{uuid.uuid4().hex[:8]}"
    raw = json.dumps(
        [
            {
                "subject_kind": customer_kind,
                "subject": "Acme Corp",
                "predicate": "reported",
                "object_kind": issue_kind,
                "object": "late shipment",
                "confidence": 0.87,
                "evidence": "Acme reported late shipments.",
                "properties": {"extractor": "test"},
            }
        ]
    )
    try:
        query = (
            f"SELECT *, '42'::text AS source_pk, 'body'::text AS source_column, "
            f"'{query_id}'::uuid AS query_id, '{graph}'::text AS graph_id "
            f"FROM rvbbit.triples_json_rows('{raw}'::jsonb)"
        )
        n = rvbbit.execute(
            """
            SELECT rvbbit.kg_ingest_triples(
                %s,
                source_table => NULL,
                match_threshold => 0.0,
                graph => %s
            )
            """,
            (query, graph),
        ).fetchone()[0]
        assert n == 1

        edge = rvbbit.execute(
            """
            SELECT n1.graph_id, n1.kind, n1.label, e.graph_id, e.predicate,
                   n2.graph_id, n2.kind, n2.label, e.confidence,
                   e.properties->>'extractor'
            FROM rvbbit.kg_edges e
            JOIN rvbbit.kg_nodes n1 ON n1.node_id = e.subject_node_id
            JOIN rvbbit.kg_nodes n2 ON n2.node_id = e.object_node_id
            WHERE n1.kind = %s AND n2.kind = %s AND e.graph_id = %s
            """,
            (customer_kind, issue_kind, graph),
        ).fetchone()
        assert edge == (
            graph,
            customer_kind,
            "Acme Corp",
            graph,
            "reported",
            graph,
            issue_kind,
            "late shipment",
            0.87,
            "test",
        )

        evidence = rvbbit.execute(
            """
            SELECT ev.graph_id, ev.source_pk, ev.source_column, ev.evidence_text, ev.query_id
            FROM rvbbit.kg_evidence ev
            JOIN rvbbit.kg_edges e ON e.edge_id = ev.edge_id
            JOIN rvbbit.kg_nodes n ON n.node_id = e.subject_node_id
            WHERE n.kind = %s AND ev.graph_id = %s
            """,
            (customer_kind, graph),
        ).fetchone()
        assert evidence[0:4] == (graph, "42", "body", "Acme reported late shipments.")
        assert str(evidence[4]) == str(query_id)
    finally:
        _cleanup_kind(rvbbit, customer_kind)
        _cleanup_kind(rvbbit, issue_kind)


def test_kg_ingest_table_validates_limit(rvbbit):
    try:
        rvbbit.execute(
            """
            SELECT *
            FROM rvbbit.kg_ingest_table(
                'pg_class'::regclass,
                'oid',
                'relname',
                limit_rows => 0
            )
            """
        )
    except Exception as exc:
        assert "limit_rows must be positive" in str(exc)
    else:
        raise AssertionError("kg_ingest_table accepted a non-positive limit_rows")
