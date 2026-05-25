"""rvbbit.text_evidence — sentence-level relevance snippets.

Lightweight inline matcher (no index needed). For richer scoring see
RYR-293 (Tantivy sidecar).
"""


def _ev(rvbbit, text, query, n=3):
    row = rvbbit.execute(
        "SELECT rvbbit.text_evidence(%s, %s, %s)", (text, query, n)
    ).fetchone()
    return row[0]


def test_finds_relevant_sentence(rvbbit):
    text = (
        "The weather is nice today. "
        "Angry customer wants a refund immediately. "
        "Bye for now."
    )
    out = _ev(rvbbit, text, "angry refund", 2)
    assert any("Angry customer" in s for s in out)


def test_returns_in_document_order(rvbbit):
    text = (
        "Refund please. "
        "Some boring intro sentence. "
        "Angry customer here. "
        "Another irrelevant note."
    )
    out = _ev(rvbbit, text, "angry refund", 3)
    # Two matches should be in original order even though scores differ.
    assert out[0].startswith("Refund")
    assert out[1].startswith("Angry")


def test_no_match_returns_empty(rvbbit):
    out = _ev(rvbbit, "Nothing to see here.", "xyz")
    assert out == []


def test_empty_inputs(rvbbit):
    assert _ev(rvbbit, "", "x") == []
    assert _ev(rvbbit, "x", "") == []


def test_top_n_zero_returns_empty(rvbbit):
    assert _ev(rvbbit, "Apples and oranges. Apples again.", "apples", n=0) == []


def test_multi_term_query_prefers_coverage(rvbbit):
    text = (
        "Apples are red. "
        "Apples and oranges and bananas all grow on trees. "
        "Bananas alone here."
    )
    out = _ev(rvbbit, text, "apples bananas oranges", 1)
    # The middle sentence covers all three terms — best match by coverage.
    assert len(out) == 1
    assert "Apples and oranges" in out[0]


def test_composes_with_means_select(rvbbit):
    """Smoke test that text_evidence works as a column expression next to
    other rvbbit functions over an actual table."""
    rvbbit.execute("DROP TABLE IF EXISTS ev_demo")
    rvbbit.execute("CREATE TABLE ev_demo (id int, body text) USING rvbbit")
    rvbbit.execute(
        "INSERT INTO ev_demo VALUES "
        "(1, 'Intro line. Customer is upset about billing. Closing line.'),"
        "(2, 'Nothing interesting here at all.')"
    )
    rvbbit.execute("SELECT rvbbit.export_to_parquet('ev_demo'::regclass)")
    try:
        rows = rvbbit.execute(
            "SELECT id, rvbbit.text_evidence(body, 'customer billing', 1) "
            "FROM ev_demo ORDER BY id"
        ).fetchall()
        # Row 1 should produce one match; row 2 should produce none.
        assert rows[0][1] == ['Customer is upset about billing.']
        assert rows[1][1] == []
    finally:
        rvbbit.execute("DROP TABLE ev_demo")
