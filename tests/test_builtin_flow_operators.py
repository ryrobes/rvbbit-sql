"""Built-in flow operators (Loop 19) — clean_year, redact, headline.

Three general-purpose built-ins ported from the LarsQL corpus, each
showcasing one semantic-flow feature:
  clean_year — retry-validated 4-digit year extraction
  redact     — PII stripping with a blocking no-email post-ward
  headline   — 3 takes + an LLM evaluator picks the punchiest

Schema tests run dry; the operator tests make live model calls.
"""

import os
import re
import uuid

import pytest

LIVE = os.environ.get("RUN_LLM_TESTS") == "1"


# ---- seeded with the right flow config -----------------------------------


def test_clean_year_seeded_with_retry(rvbbit):
    row = rvbbit.execute(
        "SELECT return_type, retry->'until'->>'sql' "
        "FROM rvbbit.operators WHERE name = 'clean_year'"
    ).fetchone()
    assert row is not None, "clean_year not seeded"
    assert row[0] == "text"
    assert "$output" in (row[1] or "")


def test_redact_seeded_with_post_ward(rvbbit):
    row = rvbbit.execute(
        "SELECT wards->'post'->0->>'mode' "
        "FROM rvbbit.operators WHERE name = 'redact'"
    ).fetchone()
    assert row is not None, "redact not seeded"
    assert row[0] == "blocking"


def test_headline_seeded_with_takes(rvbbit):
    row = rvbbit.execute(
        "SELECT (takes->>'factor')::int, takes->>'reduce' "
        "FROM rvbbit.operators WHERE name = 'headline'"
    ).fetchone()
    assert row is not None, "headline not seeded"
    assert row[0] == 3
    assert row[1] == "evaluator"


def test_flow_builtins_have_sql_wrappers(rvbbit):
    """create_operator generated the typed rvbbit.<op>(text) wrappers."""
    n = rvbbit.execute(
        "SELECT count(DISTINCT proname) FROM pg_proc "
        "WHERE pronamespace = 'rvbbit'::regnamespace "
        "  AND proname IN ('clean_year', 'redact', 'headline')"
    ).fetchone()[0]
    assert n == 3


# ---- live behavior -------------------------------------------------------


@pytest.mark.skipif(not LIVE, reason="set RUN_LLM_TESTS=1 to run")
def test_clean_year_extracts_year(rvbbit):
    marker = uuid.uuid4().hex
    row = rvbbit.execute(
        "SELECT rvbbit.clean_year(%s)",
        (f"(ref {marker}) It happened back in the summer of 1997, near the lake.",),
    ).fetchone()
    assert row[0].strip() == "1997"


@pytest.mark.skipif(not LIVE, reason="set RUN_LLM_TESTS=1 to run")
def test_clean_year_unknown_when_no_year(rvbbit):
    marker = uuid.uuid4().hex
    row = rvbbit.execute(
        "SELECT rvbbit.clean_year(%s)",
        (f"(ref {marker}) We were walking in the woods and heard a strange noise.",),
    ).fetchone()
    # the retry validator accepts a 4-digit year OR exactly 'unknown'
    assert re.match(r"^((1[6-9]|20)[0-9]{2}|unknown)$", row[0].strip())


@pytest.mark.skipif(not LIVE, reason="set RUN_LLM_TESTS=1 to run")
def test_redact_removes_email_and_passes_ward(rvbbit):
    marker = uuid.uuid4().hex
    row = rvbbit.execute(
        "SELECT rvbbit.redact(%s)",
        (f"Contact Jane Doe at jane.doe@example.com about case {marker}.",),
    ).fetchone()
    # non-empty => the no-email post-ward passed => the email was removed
    assert row[0] != "", "post-ward blocked — email was not redacted"
    assert "@example.com" not in row[0]
    assert "jane.doe" not in row[0].lower()


@pytest.mark.skipif(not LIVE, reason="set RUN_LLM_TESTS=1 to run")
def test_headline_takes_ensemble(rvbbit):
    marker = uuid.uuid4().hex
    row = rvbbit.execute(
        "SELECT rvbbit.headline(%s)",
        (
            f"(ref {marker}) A hiker reported a seven-foot creature crossing "
            "the trail at dusk before vanishing into dense brush.",
        ),
    ).fetchone()
    assert row[0] and len(row[0]) > 5
    # one headline call audits the whole ensemble — 3 takes (+ evaluator)
    n = rvbbit.execute(
        "SELECT jsonb_array_length(sub_calls) FROM rvbbit.receipts "
        "WHERE operator = 'headline' ORDER BY invocation_at DESC LIMIT 1"
    ).fetchone()[0]
    assert n >= 3
