"""EXPLAIN SEMANTIC — RYR-290 v1 (static analyzer).

Verifies the scaffold: rvbbit.<op>(...) call detection, operator-metadata
lookup, literal-arg token estimation, bitmap cache inventory. Numeric
cost / latency / dollar estimates are stubbed in this v1 and are not
asserted on here.
"""
import uuid


def _report(rvbbit, sql: str) -> list[str]:
    rows = rvbbit.execute(
        "SELECT line FROM rvbbit.explain_semantic(%s)", (sql,)
    ).fetchall()
    return [r[0] for r in rows]


def test_report_includes_header_and_query(rvbbit):
    lines = _report(rvbbit, "SELECT 1")
    text = "\n".join(lines)
    assert "Semantic Plan" in text
    assert "Query:" in text
    assert "SELECT 1" in text


def test_no_rvbbit_ops_means_zero_detected(rvbbit):
    lines = _report(rvbbit, "SELECT count(*) FROM pg_class")
    text = "\n".join(lines)
    assert "Semantic operators detected: 0" in text


def test_detects_rvbbit_means_call(rvbbit):
    lines = _report(
        rvbbit,
        "SELECT * FROM ticks WHERE rvbbit.means(body, 'angry customer')",
    )
    text = "\n".join(lines)
    assert "Semantic operators detected: 1" in text
    assert "rvbbit.means" in text
    # 'means' IS in rvbbit.operators by default, so we get its shape/return.
    assert "shape=" in text and "return=" in text


def test_unknown_rvbbit_func_is_flagged_but_listed(rvbbit):
    lines = _report(
        rvbbit,
        "SELECT rvbbit.totally_fake_function(x, y) FROM tt",
    )
    text = "\n".join(lines)
    assert "rvbbit.totally_fake_function" in text
    assert "not in rvbbit.operators" in text


def test_string_literals_in_args_contribute_tokens(rvbbit):
    lines = _report(
        rvbbit,
        "SELECT * FROM t WHERE rvbbit.means(body, "
        "'this is a longer phrase that should produce several tokens')",
    )
    text = "\n".join(lines)
    # tiktoken cl100k_base on this phrase is ~10-12 tokens; assert >= 5 to
    # avoid brittle exact-count coupling.
    import re
    m = re.search(r"estimated literal-arg tokens per row \(cl100k_base\): (\d+)", text)
    assert m is not None, f"no token line: {text}"
    assert int(m.group(1)) >= 5


def test_no_string_literals_reports_zero(rvbbit):
    lines = _report(rvbbit, "SELECT rvbbit.means(body, criterion) FROM t")
    text = "\n".join(lines)
    assert "no string literals found" in text or "estimated literal-arg tokens per row: 0" in text


def test_ignores_rvbbit_substring_in_string_literal(rvbbit):
    # The string literal contains 'rvbbit.means(...)' but it's just text —
    # the scanner must not detect it as a call.
    lines = _report(rvbbit, "SELECT 'see rvbbit.means(...)' AS doc")
    text = "\n".join(lines)
    assert "Semantic operators detected: 0" in text


def test_lists_bitmap_cache_entry(rvbbit):
    # Make a table, compact, populate a bitmap, then EXPLAIN SEMANTIC over
    # an unrelated query — the bitmap inventory should still appear (it's
    # global, since v1 doesn't yet scope by table referenced in query).
    t = f"esem_t_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(f"CREATE TABLE {t} (id int, body text) USING rvbbit")
    for i in range(10):
        rvbbit.execute(f"INSERT INTO {t} VALUES (%s, %s)", (i, f"body-{i}"))
    rvbbit.execute(f"SELECT rvbbit.export_to_parquet('{t}'::regclass)")
    rvbbit.execute(
        "SELECT rvbbit.bitmap_populate(%s::regclass::oid, %s, %s, %s)",
        (t, "even_id", "test-v1", "id % 2 = 0"),
    )
    try:
        lines = _report(rvbbit, "SELECT 1")
        text = "\n".join(lines)
        assert "Bitmap cache entries:" in text
        assert "even_id" in text
        assert "test-v1" in text
    finally:
        rvbbit.execute(f"DROP TABLE {t}")


def test_nested_parens_in_args_dont_break_scanner(rvbbit):
    lines = _report(
        rvbbit,
        "SELECT * FROM t WHERE rvbbit.means(concat(body, ' tail'), 'criterion')",
    )
    text = "\n".join(lines)
    assert "Semantic operators detected: 1" in text
    assert "arg count: 2" in text
