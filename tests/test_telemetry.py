"""Specialist + LLM telemetry views + health probe (Loop 14).

The usage views aggregate from rvbbit.receipts.sub_calls in real time
(zero storage cost). The health UDF probes each registered specialist's
/health endpoint via reqwest.
"""
import uuid

import pytest


def test_specialist_usage_view_exists(rvbbit):
    row = rvbbit.execute(
        "SELECT count(*) FROM information_schema.views "
        "WHERE table_schema = 'rvbbit' AND table_name = 'specialist_usage'"
    ).fetchone()
    assert row[0] == 1


def test_llm_usage_view_exists(rvbbit):
    row = rvbbit.execute(
        "SELECT count(*) FROM information_schema.views "
        "WHERE table_schema = 'rvbbit' AND table_name = 'llm_usage'"
    ).fetchone()
    assert row[0] == 1


def test_env_present_only_reports_presence(rvbbit):
    assert rvbbit.execute("SELECT rvbbit.env_present('PATH')").fetchone()[0] is True
    missing = f"RVBBIT_NO_SUCH_ENV_{uuid.uuid4().hex}"
    assert rvbbit.execute("SELECT rvbbit.env_present(%s)", (missing,)).fetchone()[0] is False


def test_doctor_surfaces_core_rows(rvbbit):
    rows = rvbbit.execute(
        "SELECT area, name, status, detail FROM rvbbit.doctor(false)"
    ).fetchall()
    keys = {(row[0], row[1]) for row in rows}
    assert ("core", "extension") in keys
    assert ("routing", "route_status") in keys
    assert ("provider", "default") in keys
    assert all(row[2] in {"ok", "warn", "error"} for row in rows)


def test_provider_doctor_flags_missing_auth_env(rvbbit):
    name = f"doctor_auth_{uuid.uuid4().hex[:8]}"
    try:
        rvbbit.execute(
            """
            SELECT rvbbit.register_backend(
              backend_name => %s,
              backend_endpoint => 'http://example.invalid/v1/chat/completions',
              backend_transport => 'openai_chat',
              backend_auth_env => %s,
              backend_opts => '{"model":"local/test"}'::jsonb)
            """,
            (name, f"RVBBIT_MISSING_{uuid.uuid4().hex}"),
        )
        row = rvbbit.execute(
            """
            SELECT status, detail->>'reason', (detail->>'auth_present')::boolean
            FROM rvbbit.provider_doctor(false)
            WHERE name = %s
            """,
            (name,),
        ).fetchone()
        assert row == ("warn", "missing_auth_env", False)
    finally:
        rvbbit.execute("DELETE FROM rvbbit.backends WHERE name = %s", (name,))
        rvbbit.execute("SELECT rvbbit.reload_backends()")


def test_provider_doctor_live_probes_stub_backend(rvbbit):
    name = f"doctor_stub_{uuid.uuid4().hex[:8]}"
    try:
        rvbbit.execute(
            "SELECT rvbbit.register_backend("
            "  backend_name => %s, backend_endpoint => 'stub://8', "
            "  backend_transport => 'stub')",
            (name,),
        )
        rvbbit.execute("SELECT rvbbit.reload_backends()")
        row = rvbbit.execute(
            """
            SELECT status, detail->'probe'->>'ok'
            FROM rvbbit.provider_doctor(true)
            WHERE name = %s
            """,
            (name,),
        ).fetchone()
        assert row == ("ok", "true")
    finally:
        rvbbit.execute("DELETE FROM rvbbit.backends WHERE name = %s", (name,))
        rvbbit.execute("SELECT rvbbit.reload_backends()")


def test_specialist_usage_columns(rvbbit):
    cols = [
        r[0] for r in rvbbit.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'rvbbit' AND table_name = 'specialist_usage' "
            "ORDER BY ordinal_position"
        ).fetchall()
    ]
    for required in [
        "specialist", "n_calls", "n_errors", "n_operators_using",
        "total_tokens_in", "total_tokens_out", "total_latency_ms",
        "avg_latency_ms", "p50_latency_ms", "p95_latency_ms",
        "first_call_at", "last_call_at",
    ]:
        assert required in cols, f"missing column {required}; got {cols}"


def test_specialist_health_returns_registered_specialists(rvbbit):
    """Register a stub specialist + verify it shows in specialist_health."""
    name = f"telem_stub_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(
        "SELECT rvbbit.register_backend("
        "  backend_name => %s, backend_endpoint => %s, backend_transport => %s)",
        (name, "stub://128", "stub"),
    )
    rvbbit.execute("SELECT rvbbit.reload_backends()")
    try:
        rows = rvbbit.execute(
            "SELECT specialist, transport, reachable, error "
            "FROM rvbbit.specialist_health() WHERE specialist = %s",
            (name,),
        ).fetchall()
        assert len(rows) == 1
        # stub://128 is not a real HTTP URL, so reachable=false + an error is expected.
        spec, transport, reachable, error = rows[0]
        assert spec == name
        assert transport == "stub"
        assert reachable is False
        assert error is not None
    finally:
        rvbbit.execute(f"DELETE FROM rvbbit.backends WHERE name = '{name}'")
        rvbbit.execute("SELECT rvbbit.reload_backends()")


def test_specialist_health_shape(rvbbit):
    """At least one row per registered specialist regardless of reachability."""
    n_specs = rvbbit.execute(
        "SELECT count(*) FROM rvbbit.backends"
    ).fetchone()[0]
    n_probed = rvbbit.execute(
        "SELECT count(*) FROM rvbbit.specialist_health()"
    ).fetchone()[0]
    assert n_probed == n_specs


def test_specialist_usage_rolls_up_sub_calls(rvbbit):
    """Insert a synthetic receipt with a specialist sub_call and verify
    the view aggregates it. Avoids needing a live specialist call."""
    rvbbit.execute(
        "INSERT INTO rvbbit.receipts "
        "(operator, inputs_hash, model, inputs, output, sub_calls, "
        " n_tokens_in, n_tokens_out, latency_ms) "
        "VALUES ('telem_test_op', "
        "        decode('deadbeef', 'hex'), 'telem_specialist', "
        "        '{}'::jsonb, 'result', "
        "        $$[{\"kind\":\"specialist\",\"step\":\"s\","
        "             \"model\":\"telem_specialist\","
        "             \"tokens_in\":10,\"tokens_out\":3,"
        "             \"latency_ms\":42}]$$::jsonb, "
        "        10, 3, 42)"
    )
    try:
        row = rvbbit.execute(
            "SELECT n_calls, total_tokens_in, total_tokens_out, total_latency_ms "
            "FROM rvbbit.specialist_usage WHERE specialist = 'telem_specialist'"
        ).fetchone()
        assert row == (1, 10, 3, 42)
    finally:
        rvbbit.execute(
            "DELETE FROM rvbbit.receipts WHERE operator = 'telem_test_op'"
        )
