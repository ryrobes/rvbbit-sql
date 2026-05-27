"""EXPLAIN SEMANTIC — semantic execution graph.

Verifies the user-facing graph renderer and its defensive static call-site
scanner. Cost, latency, and dollar estimates are intentionally not asserted
because they vary with receipt history and provider configuration.
"""


def _report(rvbbit, sql: str) -> list[str]:
    rows = rvbbit.execute(
        "SELECT line FROM rvbbit.explain_semantic(%s)", (sql,)
    ).fetchall()
    return [r[0] for r in rows]


def test_report_includes_header_and_query(rvbbit):
    lines = _report(rvbbit, "SELECT 1")
    text = "\n".join(lines)
    assert "Semantic Execution Graph" in text
    assert "Query:" in text
    assert "SELECT 1" in text


def test_no_rvbbit_ops_means_zero_detected(rvbbit):
    lines = _report(rvbbit, "SELECT count(*) FROM pg_class")
    text = "\n".join(lines)
    assert "No semantic operators in this query." in text


def test_detects_rvbbit_means_call(rvbbit):
    lines = _report(
        rvbbit,
        "SELECT * FROM ticks WHERE rvbbit.means(body, 'angry customer')",
    )
    text = "\n".join(lines)
    assert "rvbbit.means" in text
    assert "[scalar -> bool]" in text
    assert "criterion    'angry customer'" in text


def test_unknown_rvbbit_func_is_flagged_but_listed(rvbbit):
    lines = _report(
        rvbbit,
        "SELECT rvbbit.totally_fake_function(x, y) FROM tt",
    )
    text = "\n".join(lines)
    assert "rvbbit.totally_fake_function" in text
    assert "[? -> ?]" in text
    assert "no external endpoints recorded" in text


def test_string_literals_in_args_contribute_criterion(rvbbit):
    lines = _report(
        rvbbit,
        "SELECT * FROM t WHERE rvbbit.means(body, "
        "'this is a longer phrase that should produce several tokens')",
    )
    text = "\n".join(lines)
    assert "rvbbit.means" in text
    assert "this is a longer phrase" in text


def test_nonliteral_args_still_report_call_site(rvbbit):
    lines = _report(rvbbit, "SELECT rvbbit.means(body, criterion) FROM t")
    text = "\n".join(lines)
    assert "rvbbit.means" in text
    assert "criterion    'criterion'" in text


def test_ignores_rvbbit_substring_in_string_literal(rvbbit):
    # The string literal contains 'rvbbit.means(...)' but it's just text —
    # the scanner must not detect it as a call.
    lines = _report(rvbbit, "SELECT 'see rvbbit.means(...)' AS doc")
    text = "\n".join(lines)
    assert "No semantic operators in this query." in text


def test_nested_parens_in_args_dont_break_scanner(rvbbit):
    lines = _report(
        rvbbit,
        "SELECT * FROM t WHERE rvbbit.means(concat(body, ' tail'), 'criterion')",
    )
    text = "\n".join(lines)
    assert "rvbbit.means" in text
    assert "criterion    'criterion'" in text
