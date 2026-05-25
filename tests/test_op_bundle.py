"""Tier A operator bundle (RYR-303): pre-registered LLM operators.

These tests verify the OPERATORS ARE REGISTERED and the SQL wrappers
exist — they do NOT make live LLM calls. Live-call validation lives
in test_operators_live.py (opt-in via RUN_LLM_TESTS=1).
"""

BUNDLE = [
    ("classify",   "text", ["text", "categories"], 2),
    ("extract",    "text", ["text", "what"],       2),
    ("condense",   "text", ["text"],               1),
    ("sentiment",  "text", ["text"],               1),
    ("contradicts","bool", ["a", "b"],             2),
    ("supports",   "bool", ["a", "b"],             2),
    ("implies",    "bool", ["a", "b"],             2),
]


def test_all_bundle_operators_seeded(rvbbit):
    rows = rvbbit.execute(
        "SELECT name, return_type, arg_names FROM rvbbit.operators "
        "WHERE name = ANY(%s) ORDER BY name",
        ([n for n, _, _, _ in BUNDLE],),
    ).fetchall()
    by_name = {r[0]: (r[1], list(r[2])) for r in rows}
    for name, return_type, arg_names, _ in BUNDLE:
        assert name in by_name, f"operator {name} not seeded"
        rt, an = by_name[name]
        assert rt == return_type, f"{name} return_type {rt} != {return_type}"
        assert an == arg_names, f"{name} arg_names {an} != {arg_names}"


def test_wrapper_functions_generated(rvbbit):
    """Each operator gets a wrapper rvbbit.<name>(arg1, arg2, ..., opts jsonb)."""
    rows = rvbbit.execute(
        "SELECT proname, pronargs FROM pg_proc "
        "WHERE pronamespace = 'rvbbit'::regnamespace "
        "  AND proname = ANY(%s)",
        ([n for n, _, _, _ in BUNDLE],),
    ).fetchall()
    arities = {(name, n) for (name, n) in rows}
    for name, _, _, n_args in BUNDLE:
        expected = n_args + 1  # +1 for opts jsonb
        assert (name, expected) in arities, (
            f"missing wrapper {name}/{expected}; arities seen: {arities}"
        )


def test_bundle_models_are_reasonable(rvbbit):
    """Bundle ops should all default to a cheap+fast model (haiku)
    since they're invoked in per-row contexts."""
    rows = rvbbit.execute(
        "SELECT name, model FROM rvbbit.operators WHERE name = ANY(%s)",
        ([n for n, _, _, _ in BUNDLE],),
    ).fetchall()
    for name, model in rows:
        # Just check that some model is set, and prefer haiku-class for cost.
        assert model, f"{name} has empty model"
        # Don't pin to specific model name — just sanity check it's a real one.
        assert "/" in model, f"{name} model {model!r} doesn't look like provider/model"


def test_descriptions_set(rvbbit):
    rows = rvbbit.execute(
        "SELECT name, description FROM rvbbit.operators WHERE name = ANY(%s)",
        ([n for n, _, _, _ in BUNDLE],),
    ).fetchall()
    for name, desc in rows:
        assert desc and len(desc) > 10, f"{name} description too short or empty: {desc!r}"


def test_bool_operators_use_yes_no_parser(rvbbit):
    rows = rvbbit.execute(
        "SELECT name, parser FROM rvbbit.operators "
        "WHERE name IN ('contradicts','supports','implies')"
    ).fetchall()
    for name, parser in rows:
        assert parser == "yes_no", f"{name} parser {parser} should be yes_no"


def test_text_operators_use_strip_parser(rvbbit):
    rows = rvbbit.execute(
        "SELECT name, parser FROM rvbbit.operators "
        "WHERE name IN ('classify','extract','condense','sentiment')"
    ).fetchall()
    for name, parser in rows:
        assert parser == "strip", f"{name} parser {parser} should be strip"


def test_classify_template_references_args(rvbbit):
    """classify may route through LLM (user_prompt) OR the NLI specialist
    (steps[].inputs). Verify args are referenced in whichever surface is
    active so the test passes for both wirings."""
    row = rvbbit.execute(
        "SELECT user_prompt, steps FROM rvbbit.operators WHERE name = 'classify'"
    ).fetchone()
    user_prompt, steps = row
    import json
    haystack = (user_prompt or "") + json.dumps(steps or [])
    assert "{{ inputs.text }}" in haystack or "{{ text }}" in haystack
    assert "{{ inputs.categories }}" in haystack or "{{ categories }}" in haystack


def test_extract_template_references_args(rvbbit):
    """extract may route through either an LLM prompt OR a GLiNER
    specialist step. Verify it references its args in WHATEVER path is
    active (user_prompt for LLM, steps[].inputs for specialist)."""
    row = rvbbit.execute(
        "SELECT user_prompt, steps FROM rvbbit.operators WHERE name = 'extract'"
    ).fetchone()
    user_prompt, steps = row
    # Concatenate both surfaces so the assertion works regardless of
    # which path the operator is currently wired to.
    import json
    haystack = (user_prompt or "") + json.dumps(steps or [])
    assert "{{ inputs.text }}" in haystack or "{{ text }}" in haystack
    assert "{{ inputs.what }}" in haystack or "{{ what }}" in haystack
