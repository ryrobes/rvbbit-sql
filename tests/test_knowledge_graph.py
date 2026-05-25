"""SQL-native knowledge graph primitives."""

import uuid


def _kind(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _cleanup_kind(rvbbit, kind: str) -> None:
    rvbbit.execute("DELETE FROM rvbbit.kg_node_merges WHERE loser_kind = %s", (kind,))
    rvbbit.execute("DELETE FROM rvbbit.kg_merge_candidates WHERE kind = %s", (kind,))
    rvbbit.execute("DELETE FROM rvbbit.kg_nodes WHERE kind = %s", (kind,))


def test_kg_catalog_tables_exist(rvbbit):
    rows = rvbbit.execute(
        """
        SELECT tablename
        FROM pg_tables
        WHERE schemaname = 'rvbbit'
          AND tablename IN (
              'kg_nodes', 'kg_aliases', 'kg_edges', 'kg_evidence',
              'kg_merge_candidates', 'kg_node_merges',
              'kg_extraction_runs', 'kg_extraction_errors'
          )
        ORDER BY tablename
        """
    ).fetchall()
    assert [r[0] for r in rows] == [
        "kg_aliases",
        "kg_edges",
        "kg_evidence",
        "kg_extraction_errors",
        "kg_extraction_runs",
        "kg_merge_candidates",
        "kg_node_merges",
        "kg_nodes",
    ]
    cols = {
        r[0]
        for r in rvbbit.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'rvbbit'
              AND table_name = 'kg_evidence'
            """
        ).fetchall()
    }
    assert "query_id" in cols
    assert "graph_id" in cols


def test_kg_graphs_isolate_same_label_and_edges(rvbbit):
    account_kind = _kind("kg_account")
    issue_kind = _kind("kg_issue")
    graph_a = f"graph_a_{uuid.uuid4().hex[:8]}"
    graph_b = f"graph_b_{uuid.uuid4().hex[:8]}"
    try:
        node_a = rvbbit.execute(
            "SELECT rvbbit.kg_assert_node(%s, 'Acme Corp', '{}'::jsonb, 1.0, '', 0.0, %s)",
            (account_kind, graph_a),
        ).fetchone()[0]
        node_b = rvbbit.execute(
            "SELECT rvbbit.kg_assert_node(%s, 'Acme Corp', '{}'::jsonb, 1.0, '', 0.0, %s)",
            (account_kind, graph_b),
        ).fetchone()[0]
        assert node_a != node_b

        assert rvbbit.execute(
            "SELECT node_id FROM rvbbit.kg_resolve_node(%s, 'Acme Corp', '', 0.0, %s)",
            (account_kind, graph_a),
        ).fetchone()[0] == node_a
        assert rvbbit.execute(
            "SELECT node_id FROM rvbbit.kg_resolve_node(%s, 'Acme Corp', '', 0.0, %s)",
            (account_kind, graph_b),
        ).fetchone()[0] == node_b

        rvbbit.execute(
            """
            SELECT rvbbit.kg_assert_edge(
                %s, 'Acme Corp', 'reported', %s, 'late shipment',
                1.0, '{}'::jsonb, '{}'::jsonb, '', 0.0, %s
            )
            """,
            (account_kind, issue_kind, graph_a),
        )
        rvbbit.execute(
            """
            SELECT rvbbit.kg_assert_edge(
                %s, 'Acme Corp', 'reported', %s, 'billing issue',
                1.0, '{}'::jsonb, '{}'::jsonb, '', 0.0, %s
            )
            """,
            (account_kind, issue_kind, graph_b),
        )

        neighbors_a = rvbbit.execute(
            """
            SELECT to_label
            FROM rvbbit.kg_neighbors(%s, 'Acme Corp', 1, 'out', '', 0.0, %s)
            """,
            (account_kind, graph_a),
        ).fetchall()
        neighbors_b = rvbbit.execute(
            """
            SELECT to_label
            FROM rvbbit.kg_neighbors(%s, 'Acme Corp', 1, 'out', '', 0.0, %s)
            """,
            (account_kind, graph_b),
        ).fetchall()
        assert neighbors_a == [("late shipment",)]
        assert neighbors_b == [("billing issue",)]
    finally:
        _cleanup_kind(rvbbit, account_kind)
        _cleanup_kind(rvbbit, issue_kind)


def test_kg_assert_node_is_idempotent_and_normalizes_aliases(rvbbit):
    kind = _kind("kg_company")
    try:
        first = rvbbit.execute(
            "SELECT rvbbit.kg_assert_node(%s, %s, %s, 0.7, '', 0.0)",
            (kind, "  Acme   Corp  ", '{"source":"test"}'),
        ).fetchone()[0]
        second = rvbbit.execute(
            "SELECT rvbbit.kg_assert_node(%s, %s, '{}'::jsonb, 0.9, '', 0.0)",
            (kind, "acme corp"),
        ).fetchone()[0]
        assert first == second

        row = rvbbit.execute(
            """
            SELECT kind, label_norm, confidence, properties->>'source'
            FROM rvbbit.kg_nodes
            WHERE node_id = %s
            """,
            (first,),
        ).fetchone()
        assert row == (kind, "acme corp", 0.9, "test")

        resolved = rvbbit.execute(
            "SELECT node_id, match_method FROM rvbbit.kg_resolve_node(%s, %s, '', 0.0)",
            (kind, "ACME CORP"),
        ).fetchone()
        assert resolved == (first, "alias")
    finally:
        _cleanup_kind(rvbbit, kind)


def test_kg_alias_can_resolve_alternate_surface_form(rvbbit):
    kind = _kind("kg_org")
    try:
        node_id = rvbbit.execute(
            "SELECT rvbbit.kg_assert_node(%s, 'OpenAI Incorporated', '{}'::jsonb, 1.0, '', 0.0)",
            (kind,),
        ).fetchone()[0]
        alias_id = rvbbit.execute(
            "SELECT rvbbit.kg_assert_alias(%s, 'Open AI', 0.95)",
            (node_id,),
        ).fetchone()[0]
        assert alias_id is not None

        resolved = rvbbit.execute(
            "SELECT node_id, label, match_method FROM rvbbit.kg_resolve_node(%s, 'open ai', '', 0.0)",
            (kind,),
        ).fetchone()
        assert resolved == (node_id, "OpenAI Incorporated", "alias")
    finally:
        _cleanup_kind(rvbbit, kind)


def test_kg_assert_edge_records_evidence_and_neighbors(rvbbit):
    cust_kind = _kind("kg_customer")
    issue_kind = _kind("kg_issue")
    try:
        query_id = rvbbit.execute("SELECT rvbbit.reset_query_id()").fetchone()[0]
        edge_id = rvbbit.execute(
            """
            SELECT rvbbit.kg_assert_edge(
                %s, 'Acme Corp',
                'reported',
                %s, 'late shipment',
                0.91,
                '{"text":"Acme reported late shipments in Q4.","source":"ticket"}'::jsonb,
                '{"channel":"support"}'::jsonb,
                '',
                0.0
            )
            """,
            (cust_kind, issue_kind),
        ).fetchone()[0]

        evidence = rvbbit.execute(
            """
            SELECT evidence_text, properties->>'source', query_id
            FROM rvbbit.kg_evidence
            WHERE edge_id = %s
            """,
            (edge_id,),
        ).fetchone()
        assert evidence[0:2] == ("Acme reported late shipments in Q4.", "ticket")
        assert str(evidence[2]) == str(query_id)

        neighbors = rvbbit.execute(
            """
            SELECT predicate, to_kind, to_label, confidence, properties->>'channel'
            FROM rvbbit.kg_neighbors(%s, 'Acme Corp', 1, 'out', '', 0.0)
            """,
            (cust_kind,),
        ).fetchall()
        assert neighbors == [
            ("reported", issue_kind, "late shipment", 0.91, "support")
        ]
    finally:
        _cleanup_kind(rvbbit, cust_kind)
        _cleanup_kind(rvbbit, issue_kind)


def test_kg_link_evidence_accepts_explicit_query_id(rvbbit):
    cust_kind = _kind("kg_customer")
    issue_kind = _kind("kg_issue")
    query_id = uuid.uuid4()
    try:
        edge_id = rvbbit.execute(
            "SELECT rvbbit.kg_assert_edge(%s, 'Acme', 'reported', %s, 'late shipment', 1.0, '{}'::jsonb, '{}'::jsonb, '', 0.0)",
            (cust_kind, issue_kind),
        ).fetchone()[0]
        evidence_id = rvbbit.execute(
            """
            SELECT rvbbit.kg_link_evidence(
                target_edge_id => %s,
                evidence_text => 'Acme reported late shipment.',
                properties => %s::jsonb
            )
            """,
            (edge_id, f'{{"query_id":"{query_id}"}}'),
        ).fetchone()[0]
        row = rvbbit.execute(
            "SELECT query_id, evidence_text FROM rvbbit.kg_evidence WHERE evidence_id = %s",
            (evidence_id,),
        ).fetchone()
        assert str(row[0]) == str(query_id)
        assert row[1] == "Acme reported late shipment."
    finally:
        _cleanup_kind(rvbbit, cust_kind)
        _cleanup_kind(rvbbit, issue_kind)


def test_kg_suggest_and_reject_merge_candidate(rvbbit):
    kind = _kind("kg_vendor")
    try:
        left = rvbbit.execute(
            "SELECT rvbbit.kg_assert_node(%s, 'Acme Corp', '{}'::jsonb, 0.8, '', 0.0)",
            (kind,),
        ).fetchone()[0]
        right = rvbbit.execute(
            "SELECT rvbbit.kg_assert_node(%s, 'Acme Corporation', '{}'::jsonb, 0.7, '', 0.0)",
            (kind,),
        ).fetchone()[0]

        row = rvbbit.execute(
            """
            SELECT candidate_id, left_node_id, right_node_id, score, status
            FROM rvbbit.kg_suggest_merges(%s, 0.85, 10)
            """,
            (kind,),
        ).fetchone()
        assert row is not None
        assert {row[1], row[2]} == {left, right}
        assert row[3] >= 0.85
        assert row[4] == "pending"

        rejected = rvbbit.execute(
            "SELECT rvbbit.kg_reject_merge(%s)",
            (row[0],),
        ).fetchone()[0]
        assert rejected == row[0]

        rows_after_reject = rvbbit.execute(
            "SELECT candidate_id FROM rvbbit.kg_suggest_merges(%s, 0.85, 10)",
            (kind,),
        ).fetchall()
        assert rows_after_reject == []

        status = rvbbit.execute(
            "SELECT status FROM rvbbit.kg_merge_candidates WHERE candidate_id = %s",
            (row[0],),
        ).fetchone()[0]
        assert status == "rejected"
    finally:
        _cleanup_kind(rvbbit, kind)


def test_kg_accept_merge_rewires_edges_aliases_and_evidence(rvbbit):
    customer_kind = _kind("kg_customer")
    issue_kind = _kind("kg_issue")
    try:
        winner = rvbbit.execute(
            "SELECT rvbbit.kg_assert_node(%s, 'Acme Corp', '{\"winner_node\":\"yes\"}'::jsonb, 0.9, '', 0.0)",
            (customer_kind,),
        ).fetchone()[0]
        loser = rvbbit.execute(
            "SELECT rvbbit.kg_assert_node(%s, 'Acme Corporation', '{\"loser_node\":\"yes\"}'::jsonb, 0.7, '', 0.0)",
            (customer_kind,),
        ).fetchone()[0]

        qid1 = rvbbit.execute("SELECT rvbbit.reset_query_id()").fetchone()[0]
        rvbbit.execute(
            """
            SELECT rvbbit.kg_assert_edge(
                %s, 'Acme Corp',
                'reported',
                %s, 'late shipment',
                0.8,
                '{"text":"winner evidence"}'::jsonb,
                '{"winner_edge":"yes"}'::jsonb,
                '',
                0.0
            )
            """,
            (customer_kind, issue_kind),
        )

        qid2 = rvbbit.execute("SELECT rvbbit.reset_query_id()").fetchone()[0]
        rvbbit.execute(
            """
            SELECT rvbbit.kg_assert_edge(
                %s, 'Acme Corporation',
                'reported',
                %s, 'late shipment',
                0.75,
                '{"text":"loser evidence"}'::jsonb,
                '{"loser_edge":"yes"}'::jsonb,
                '',
                0.0
            )
            """,
            (customer_kind, issue_kind),
        )

        candidate_id = rvbbit.execute(
            "SELECT candidate_id FROM rvbbit.kg_suggest_merges(%s, 0.85, 10)",
            (customer_kind,),
        ).fetchone()[0]
        merge_id = rvbbit.execute(
            "SELECT rvbbit.kg_accept_merge(%s, %s)",
            (candidate_id, winner),
        ).fetchone()[0]
        assert merge_id is not None

        loser_exists = rvbbit.execute(
            "SELECT count(*) FROM rvbbit.kg_nodes WHERE node_id = %s",
            (loser,),
        ).fetchone()[0]
        assert loser_exists == 0

        resolved = rvbbit.execute(
            "SELECT node_id, match_method FROM rvbbit.kg_resolve_node(%s, 'Acme Corporation', '', 0.0)",
            (customer_kind,),
        ).fetchone()
        assert resolved == (winner, "alias")

        candidate = rvbbit.execute(
            """
            SELECT status, properties->>'merge_id', properties->>'winner_node_id', properties->>'loser_node_id'
            FROM rvbbit.kg_merge_candidates
            WHERE candidate_id = %s
            """,
            (candidate_id,),
        ).fetchone()
        assert candidate == ("accepted", str(merge_id), str(winner), str(loser))

        merge_row = rvbbit.execute(
            """
            SELECT winner_node_id, loser_node_id, loser_label
            FROM rvbbit.kg_node_merges
            WHERE merge_id = %s
            """,
            (merge_id,),
        ).fetchone()
        assert merge_row == (winner, loser, "Acme Corporation")

        edge = rvbbit.execute(
            """
            SELECT e.edge_id, e.properties->>'winner_edge', e.properties->>'loser_edge'
            FROM rvbbit.kg_edges e
            JOIN rvbbit.kg_nodes subj ON subj.node_id = e.subject_node_id
            JOIN rvbbit.kg_nodes obj ON obj.node_id = e.object_node_id
            WHERE subj.node_id = %s
              AND obj.kind = %s
              AND obj.label = 'late shipment'
              AND e.predicate = 'reported'
            """,
            (winner, issue_kind),
        ).fetchall()
        assert len(edge) == 1
        assert edge[0][1:] == ("yes", "yes")

        evidence = rvbbit.execute(
            """
            SELECT evidence_text, query_id
            FROM rvbbit.kg_evidence
            WHERE edge_id = %s
            ORDER BY evidence_text
            """,
            (edge[0][0],),
        ).fetchall()
        assert evidence == [
            ("loser evidence", qid2),
            ("winner evidence", qid1),
        ]
    finally:
        _cleanup_kind(rvbbit, customer_kind)
        _cleanup_kind(rvbbit, issue_kind)


def test_kg_paths_find_short_connection(rvbbit):
    a_kind = _kind("kg_account")
    issue_kind = _kind("kg_issue")
    metric_kind = _kind("kg_metric")
    try:
        rvbbit.execute(
            "SELECT rvbbit.kg_assert_edge(%s, 'Acme', 'reported', %s, 'late shipment', 1.0, '{}'::jsonb, '{}'::jsonb, '', 0.0)",
            (a_kind, issue_kind),
        )
        rvbbit.execute(
            "SELECT rvbbit.kg_assert_edge(%s, 'late shipment', 'affects', %s, 'retention risk', 1.0, '{}'::jsonb, '{}'::jsonb, '', 0.0)",
            (issue_kind, metric_kind),
        )

        paths = rvbbit.execute(
            """
            SELECT length, labels
            FROM rvbbit.kg_paths(%s, 'Acme', %s, 'retention risk', 3, 'out', '', 0.0)
            """,
            (a_kind, metric_kind),
        ).fetchall()
        assert paths == [(2, ["Acme", "late shipment", "retention risk"])]
    finally:
        _cleanup_kind(rvbbit, a_kind)
        _cleanup_kind(rvbbit, issue_kind)
        _cleanup_kind(rvbbit, metric_kind)


def test_kg_context_returns_ranked_evidence_neighborhood(rvbbit):
    customer_kind = _kind("kg_customer")
    issue_kind = _kind("kg_issue")
    metric_kind = _kind("kg_metric")
    product_kind = _kind("kg_product")
    try:
        qid1 = rvbbit.execute("SELECT rvbbit.reset_query_id()").fetchone()[0]
        reported_edge = rvbbit.execute(
            """
            SELECT rvbbit.kg_assert_edge(
                %s, 'Acme Corp',
                'reported',
                %s, 'late shipment',
                0.9,
                '{"text":"Acme reported late shipments.","source":"ticket"}'::jsonb,
                '{"channel":"support"}'::jsonb,
                '',
                0.0
            )
            """,
            (customer_kind, issue_kind),
        ).fetchone()[0]

        qid2 = rvbbit.execute("SELECT rvbbit.reset_query_id()").fetchone()[0]
        affects_edge = rvbbit.execute(
            """
            SELECT rvbbit.kg_assert_edge(
                %s, 'late shipment',
                'affects',
                %s, 'retention risk',
                0.5,
                '{"text":"Late shipments increased retention risk."}'::jsonb,
                '{}'::jsonb,
                '',
                0.0
            )
            """,
            (issue_kind, metric_kind),
        ).fetchone()[0]

        metric_node = rvbbit.execute(
            "SELECT node_id FROM rvbbit.kg_resolve_node(%s, 'retention risk', '', 0.0)",
            (metric_kind,),
        ).fetchone()[0]
        rvbbit.execute(
            """
            SELECT rvbbit.kg_link_evidence(
                target_node_id => %s,
                evidence_text => 'Retention risk is a tracked account metric.',
                confidence => 0.7
            )
            """,
            (metric_node,),
        )

        rvbbit.execute(
            """
            SELECT rvbbit.kg_assert_edge(
                %s, 'Rvbbit',
                'used_by',
                %s, 'Acme Corp',
                0.95,
                '{}'::jsonb,
                '{}'::jsonb,
                '',
                0.0
            )
            """,
            (product_kind, customer_kind),
        )

        rows = rvbbit.execute(
            """
            SELECT context_rank, depth, edge_id, predicate, to_label, edge_direction,
                   round(score::numeric, 4)::float8, evidence_count, evidence,
                   path_edge_ids
            FROM rvbbit.kg_context(%s, 'Acme Corp', 2, 10, 'out', true, '', 0.0)
            """,
            (customer_kind,),
        ).fetchall()

        assert [(r[0], r[1], r[3], r[4], r[5]) for r in rows] == [
            (1, 1, "reported", "late shipment", "out"),
            (2, 2, "affects", "retention risk", "out"),
        ]
        assert rows[0][2] == reported_edge
        assert rows[0][6] == 0.9
        assert rows[0][7] == 1
        assert rows[0][8][0]["evidence_text"] == "Acme reported late shipments."
        assert rows[0][8][0]["query_id"] == str(qid1)
        assert rows[0][9] == [reported_edge]

        assert rows[1][2] == affects_edge
        assert rows[1][6] == 0.3825
        assert rows[1][7] == 2
        assert [item["target"] for item in rows[1][8]] == ["edge", "to_node"]
        assert rows[1][8][0]["query_id"] == str(qid2)
        assert rows[1][9] == [reported_edge, affects_edge]

        custom_decay = rvbbit.execute(
            """
            SELECT predicate, depth, round(score::numeric, 4)::float8
            FROM rvbbit.kg_context(
                %s, 'Acme Corp', 2, 10, 'out', true, '', 0.0,
                ranking => '{"depth_decay":0.5}'::jsonb
            )
            ORDER BY context_rank
            """,
            (customer_kind,),
        ).fetchall()
        assert custom_decay == [
            ("reported", 1, 0.9),
            ("affects", 2, 0.225),
        ]

        without_evidence = rvbbit.execute(
            """
            SELECT evidence_count, evidence
            FROM rvbbit.kg_context(%s, 'Acme Corp', 1, 1, 'out', false, '', 0.0)
            """,
            (customer_kind,),
        ).fetchone()
        assert without_evidence == (0, [])

        incoming = rvbbit.execute(
            """
            SELECT predicate, from_kind, from_label, to_kind, to_label, edge_direction
            FROM rvbbit.kg_context(%s, 'Acme Corp', 1, 10, 'in', false, '', 0.0)
            """,
            (customer_kind,),
        ).fetchall()
        assert incoming == [
            ("used_by", customer_kind, "Acme Corp", product_kind, "Rvbbit", "in")
        ]
    finally:
        _cleanup_kind(rvbbit, customer_kind)
        _cleanup_kind(rvbbit, issue_kind)
        _cleanup_kind(rvbbit, metric_kind)
        _cleanup_kind(rvbbit, product_kind)
