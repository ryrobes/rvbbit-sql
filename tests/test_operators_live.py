"""Live LLM tests — gated behind RUN_LLM_TESTS=1.

These make real OpenRouter calls and cost real money (~$0.0001/call).
Run with:
    RUN_LLM_TESTS=1 make test-live
"""

import os

import pytest

LIVE = os.environ.get("RUN_LLM_TESTS") == "1"
pytestmark = pytest.mark.skipif(not LIVE, reason="set RUN_LLM_TESTS=1 to run")


def test_means_returns_bool(rvbbit):
    row = rvbbit.execute(
        "SELECT rvbbit.means("
        "  'This is absolutely the worst experience I have ever had. "
        "Cancelling immediately and disputing the charge.', "
        "  'angry customer who wants to cancel')"
    ).fetchone()
    assert row[0] is True


def test_means_negative_case(rvbbit):
    row = rvbbit.execute(
        "SELECT rvbbit.means("
        "  'Just wanted to say thank you for the great support', "
        "  'angry customer who wants to cancel')"
    ).fetchone()
    assert row[0] is False


def test_about_returns_score(rvbbit):
    row = rvbbit.execute(
        "SELECT rvbbit.about("
        "  'My credit card was charged twice this month, I need a refund', "
        "  'billing problems')"
    ).fetchone()
    assert 0.7 <= row[0] <= 1.0


def test_summarize_returns_text(rvbbit):
    row = rvbbit.execute(
        "SELECT rvbbit.summarize("
        "  'The quarterly report shows 15 percent revenue growth driven primarily "
        "by international markets, with North America declining slightly')"
    ).fetchone()
    assert isinstance(row[0], str)
    assert len(row[0]) > 0


def test_infix_symbol_works(rvbbit):
    rvbbit.execute("SET search_path = public, rvbbit")
    row = rvbbit.execute(
        "SELECT 'I want to cancel and dispute the charges' ~~? 'angry customer'"
    ).fetchone()
    assert row[0] is True


def test_run_all_tests_passes(rvbbit):
    """Run the embedded tests on every built-in. With real LLM calls,
    each prompt and expectation pair must produce the expected outcome.
    If a future prompt edit breaks behavior, this test catches it."""
    rows = rvbbit.execute(
        "SELECT operator, test_name, passed, actual, expected, error "
        "FROM rvbbit.run_all_tests() WHERE NOT passed"
    ).fetchall()
    assert rows == [], (
        "embedded operator tests failed:\n"
        + "\n".join(
            f"  {r[0]}.{r[1]}: actual={r[3]!r} expected={r[4]!r} error={r[5]}"
            for r in rows
        )
    )


def test_safe_classify_multistep(rvbbit):
    """Multi-step operator: LLM classifies + code validator clamps to
    allowed set. End-to-end with real LLM call."""
    row = rvbbit.execute(
        "SELECT rvbbit.safe_classify("
        "  'My credit card was charged twice this month and I need a refund', "
        "  'billing,shipping,bug-report,other')"
    ).fetchone()
    assert row[0] == "billing"


def test_safe_classify_sub_calls_logged(rvbbit):
    """Receipt for multi-step op has 2 sub_calls (llm + code)."""
    import uuid as _uuid
    marker = f"unique-multistep-{_uuid.uuid4().hex}"
    rvbbit.execute(
        "SELECT rvbbit.safe_classify(%s, 'billing,shipping,other')",
        (f"This {marker} is about an invoice question",),
    )
    row = rvbbit.execute(
        "SELECT jsonb_array_length(sub_calls), n_tokens_in > 0, n_tokens_out > 0 "
        "FROM rvbbit.receipts WHERE operator = 'safe_classify' "
        "  AND inputs::text LIKE %s "
        "ORDER BY invocation_at DESC LIMIT 1",
        (f"%{marker}%",),
    ).fetchone()
    assert row is not None
    n_subs, has_tokens_in, has_tokens_out = row
    assert n_subs == 2, f"expected 2 sub_calls (llm + code), got {n_subs}"
    assert has_tokens_in, "LLM sub-call should contribute tokens"
    assert has_tokens_out


def test_safe_classify_validator_fallback(rvbbit):
    """When LLM picks something outside the allowed list, validator
    returns 'unknown' instead of garbage."""
    row = rvbbit.execute(
        "SELECT rvbbit.safe_classify("
        "  'Lorem ipsum dolor sit amet, consectetur', "
        "  'billing,shipping')"
    ).fetchone()
    # The LLM might pick one of those anyway (it's biased to comply), so
    # the assertion is "either valid or unknown" — the key invariant is
    # we never get arbitrary text.
    assert row[0] in ("billing", "shipping", "unknown")


def test_receipt_logged(rvbbit):
    # Force fresh hash so we hit the API:
    unique = f"test-{__import__('uuid').uuid4().hex}"
    rvbbit.execute(
        "SELECT rvbbit.means(%s, 'is this a unique test marker')",
        (unique,),
    ).fetchone()
    row = rvbbit.execute(
        "SELECT count(*) FROM rvbbit.receipts WHERE inputs::text LIKE %s",
        (f"%{unique}%",),
    ).fetchone()
    assert row[0] >= 1
