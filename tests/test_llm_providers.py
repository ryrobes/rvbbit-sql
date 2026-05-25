"""LLM providers as model backends (Loop 23, Phase 1).

An LLM provider is a row in rvbbit.backends with a chat transport.
`providers::chat` resolves a provider backend by name and dispatches it
through the same Transport machinery specialists use. An `llm` node can
pin a `provider`; absent, the default (openrouter) is used.

The `stub` transport echoes a chat prompt, so a stub-transport backend is
a deterministic, network-free LLM provider — the dispatch tests use one.
Live provider tests are gated behind RUN_LLM_TESTS=1.
"""

import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

LIVE = pytest.mark.skipif(
    os.environ.get("RUN_LLM_TESTS") != "1", reason="set RUN_LLM_TESTS=1 to run"
)


@pytest.fixture
def stub_provider(rvbbit):
    """A deterministic echo LLM provider — the stub transport."""
    name = f"stubllm_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(
        "SELECT rvbbit.register_backend("
        "  backend_name => %s, backend_endpoint => 'stub://4', "
        "  backend_transport => 'stub')",
        (name,),
    )
    yield name
    rvbbit.execute(f"DELETE FROM rvbbit.backends WHERE name = '{name}'")


@pytest.fixture
def openai_compatible_chat_server():
    """Tiny OpenAI-compatible chat server reachable from pg-rvbbit."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - stdlib callback name
            length = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8") or "{}")
            with self.server.lock:  # type: ignore[attr-defined]
                self.server.current += 1  # type: ignore[attr-defined]
                self.server.max_current = max(  # type: ignore[attr-defined]
                    self.server.max_current, self.server.current  # type: ignore[attr-defined]
                )
                self.server.seen.append(  # type: ignore[attr-defined]
                    {
                        "path": self.path,
                        "authorization": self.headers.get("authorization"),
                        "body": payload,
                    }
                )
            try:
                delay = self.server.delay_seconds  # type: ignore[attr-defined]
                if delay:
                    time.sleep(delay)
                model = payload.get("model") or ""
                messages = payload.get("messages") or []
                user = ""
                for msg in messages:
                    if msg.get("role") == "user":
                        user = msg.get("content") or ""
                body = {
                    "id": f"chatcmpl-{uuid.uuid4().hex}",
                    "object": "chat.completion",
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": f"local-vllm-ok model={model} user={user}",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 11,
                        "completion_tokens": 7,
                        "total_tokens": 18,
                    },
                }
                out = json.dumps(body).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
            finally:
                with self.server.lock:  # type: ignore[attr-defined]
                    self.server.current -= 1  # type: ignore[attr-defined]

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    server.seen = []  # type: ignore[attr-defined]
    server.current = 0  # type: ignore[attr-defined]
    server.max_current = 0  # type: ignore[attr-defined]
    server.delay_seconds = 0.0  # type: ignore[attr-defined]
    server.lock = threading.Lock()  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host = os.environ.get("RVBBIT_TEST_HTTP_HOST", "bench")
    yield {
        "endpoint": f"http://{host}:{server.server_port}/v1/chat/completions",
        "seen": server.seen,  # type: ignore[attr-defined]
        "server": server,
    }
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


def _make_op(rvbbit, steps, arg_names=("text",), return_type="text"):
    name = f"provop_{uuid.uuid4().hex[:8]}"
    args_sql = "ARRAY[" + ",".join(f"'{a}'" for a in arg_names) + "]"
    rvbbit.execute(
        "SELECT rvbbit.create_operator("
        f"  op_name => %s, op_arg_names => {args_sql}, "
        "  op_return_type => %s, op_steps => %s::jsonb)",
        (name, return_type, json.dumps(steps)),
    )
    return name


def _drop_op(rvbbit, name, n_args=1):
    sig = ", ".join(["text"] * n_args + ["jsonb"])
    rvbbit.execute(f"DELETE FROM rvbbit.operators WHERE name = '{name}'")
    rvbbit.execute(f"DROP FUNCTION IF EXISTS rvbbit.{name}({sig})")


# ---- the backend registry ------------------------------------------------


def test_openrouter_seeded(rvbbit):
    """The default LLM provider is pre-registered as a chat backend."""
    row = rvbbit.execute(
        "SELECT transport, endpoint_url, auth_header_env "
        "FROM rvbbit.backends WHERE name = 'openrouter'"
    ).fetchone()
    assert row is not None
    assert row[0] == "openai_chat"
    assert "openrouter.ai" in row[1]
    assert row[2] == "OPENROUTER_API_KEY"


def test_register_chat_provider(rvbbit):
    """An LLM provider registers like any backend — a chat transport."""
    name = f"vllm_{uuid.uuid4().hex[:8]}"
    try:
        rvbbit.execute(
            "SELECT rvbbit.register_backend("
            "  backend_name => %s, "
            "  backend_endpoint => 'http://localhost:8000/v1/chat/completions', "
            "  backend_transport => 'openai_chat')",
            (name,),
        )
        t = rvbbit.execute(
            "SELECT transport FROM rvbbit.backends WHERE name = %s", (name,)
        ).fetchone()[0]
        assert t == "openai_chat"
    finally:
        rvbbit.execute(f"DELETE FROM rvbbit.backends WHERE name = '{name}'")


def test_openai_compatible_local_chat_provider(rvbbit, openai_compatible_chat_server):
    """A local vLLM/Ollama-style chat endpoint works as an LLM provider."""
    name = f"vllm_{uuid.uuid4().hex[:8]}"
    model = "nvidia/Gemma-4-31B-IT-NVFP4"
    endpoint = openai_compatible_chat_server["endpoint"]
    try:
        rvbbit.execute(
            """
            SELECT rvbbit.register_backend(
              backend_name => %s,
              backend_endpoint => %s,
              backend_transport => 'openai_chat',
              backend_max_concur => 2,
              backend_timeout_ms => 5000,
              backend_opts => %s::jsonb)
            """,
            (name, endpoint, json.dumps({"model": model})),
        )
        catalog_doc = rvbbit.execute(
            """
            SELECT rvbbit.register_self_hosted_model(
              provider => 'local-vllm',
              model => %s,
              backend_name => %s,
              display_name => 'Local vLLM Gemma',
              family => 'gemma',
              capabilities => '["chat"]'::jsonb,
              context_window => 32768,
              output_token_limit => 4096,
              cost_policy => 'free',
              raw => '{"test": true}'::jsonb)
            """,
            (model, name),
        ).fetchone()[0]
        assert catalog_doc["provider"] == "local-vllm"
        assert catalog_doc["model"] == model
        assert catalog_doc["backend"] == name
        assert catalog_doc["cost_policy"] == "free"
        rvbbit.execute("SELECT rvbbit.reload_backends()")

        probe = rvbbit.execute("SELECT rvbbit.backend_probe(%s)", (name,)).fetchone()[0]
        assert probe["ok"] is True, probe
        assert "local-vllm-ok" in probe["output"]

        op = None
        try:
            op = _make_op(
                rvbbit,
                [
                    {
                        "name": "ask",
                        "kind": "llm",
                        "provider": name,
                        "model": model,
                        "system": "Reply tersely.",
                        "user": "Echo {{ inputs.text }}",
                        "max_tokens": 12,
                        "temperature": 0,
                    }
                ],
            )
            query_id = rvbbit.execute("SELECT rvbbit.reset_query_id()").fetchone()[0]
            out = rvbbit.execute(f"SELECT rvbbit.{op}('from sql')").fetchone()[0]
            assert f"model={model}" in out
            assert "Echo from sql" in out

            receipt = rvbbit.execute(
                """
                SELECT error, sub_calls->0->>'backend', sub_calls->0->>'transport',
                       sub_calls->0->>'model',
                       (sub_calls->0->>'tokens_in')::int,
                       (sub_calls->0->>'tokens_out')::int
                FROM rvbbit.receipts
                WHERE operator = %s AND query_id = %s::uuid
                ORDER BY invocation_at DESC
                LIMIT 1
                """,
                (op, str(query_id)),
            ).fetchone()
            assert receipt and receipt[0] is None
            assert receipt[1] == name
            assert receipt[2] == "openai_chat"
            assert receipt[3] == model
            assert receipt[4] == 11
            assert receipt[5] == 7

            rvbbit.execute("SELECT rvbbit.backfill_cost_events_from_receipts(1000)")
            cost = rvbbit.execute(
                """
                SELECT status, cost_source, cost_usd::float8
                FROM rvbbit.cost_latest
                WHERE query_id = %s::uuid AND backend = %s
                ORDER BY event_id DESC
                LIMIT 1
                """,
                (str(query_id), name),
            ).fetchone()
            assert cost == ("free", "policy_free", 0.0)
        finally:
            if op:
                _drop_op(rvbbit, op)

        default_op = None
        try:
            default_doc = rvbbit.execute(
                "SELECT rvbbit.set_default_provider(%s)", (name,)
            ).fetchone()[0]
            assert default_doc["default_provider"] == name
            assert rvbbit.execute("SELECT rvbbit.default_provider()").fetchone()[0] == name
            default_op = f"defaultprov_{uuid.uuid4().hex[:8]}"
            rvbbit.execute(
                """
                SELECT rvbbit.create_operator(
                  op_name => %s,
                  op_arg_names => ARRAY['text'],
                  op_return_type => 'text',
                  op_system => 'Reply tersely.',
                  op_user => 'Default {{ inputs.text }}',
                  op_model => %s,
                  op_max_tokens => 12,
                  op_temperature => 0)
                """,
                (default_op, model),
            )
            query_id = rvbbit.execute("SELECT rvbbit.reset_query_id()").fetchone()[0]
            out = rvbbit.execute(f"SELECT rvbbit.{default_op}('provider sql')").fetchone()[0]
            assert f"model={model}" in out
            assert "Default provider sql" in out
            backend = rvbbit.execute(
                """
                SELECT sub_calls->0->>'backend'
                FROM rvbbit.receipts
                WHERE operator = %s AND query_id = %s::uuid
                ORDER BY invocation_at DESC
                LIMIT 1
                """,
                (default_op, str(query_id)),
            ).fetchone()[0]
            assert backend == name
        finally:
            if default_op:
                _drop_op(rvbbit, default_op)
            rvbbit.execute("SELECT rvbbit.set_default_provider('openrouter')")

        seen = openai_compatible_chat_server["seen"]
        assert len(seen) >= 3
        assert seen[-1]["path"] == "/v1/chat/completions"
        assert seen[-1]["authorization"] is None
        assert seen[-1]["body"]["model"] == model
    finally:
        rvbbit.execute("DELETE FROM rvbbit.backends WHERE name = %s", (name,))
        rvbbit.execute("SELECT rvbbit.reload_backends()")


def test_openai_chat_respects_backend_max_concurrent(rvbbit, openai_compatible_chat_server):
    """openai_chat honors each backend's max_concurrent catalog setting."""
    name = f"vllm_limit_{uuid.uuid4().hex[:8]}"
    model = "local/test-model"
    endpoint = openai_compatible_chat_server["endpoint"]
    server = openai_compatible_chat_server["server"]
    server.delay_seconds = 0.12
    op = None
    try:
        rvbbit.execute(
            """
            SELECT rvbbit.register_backend(
              backend_name => %s,
              backend_endpoint => %s,
              backend_transport => 'openai_chat',
              backend_max_concur => 2,
              backend_timeout_ms => 5000,
              backend_opts => %s::jsonb)
            """,
            (name, endpoint, json.dumps({"model": model})),
        )
        rvbbit.execute("SELECT rvbbit.reload_backends()")
        op = _make_op(
            rvbbit,
            [
                {
                    "name": "ask",
                    "kind": "llm",
                    "provider": name,
                    "model": model,
                    "user": "row {{ inputs.text }}",
                    "max_tokens": 4,
                    "temperature": 0,
                }
            ],
        )
        stats = rvbbit.execute(
            """
            SELECT n_inputs, n_executed, n_errors
            FROM rvbbit.prewarm_operator(
              %s,
              'SELECT (''row-'' || g)::text AS text FROM generate_series(1, 6) g')
            """,
            (op,),
        ).fetchone()
        assert stats == (6, 6, 0)
        assert 1 < server.max_current <= 2
    finally:
        if op:
            _drop_op(rvbbit, op)
        rvbbit.execute("DELETE FROM rvbbit.backends WHERE name = %s", (name,))
        rvbbit.execute("SELECT rvbbit.reload_backends()")


def test_self_hosted_model_rate_cost_policy(rvbbit, openai_compatible_chat_server):
    """Self-hosted providers can attach explicit per-token cost estimates."""
    name = f"priced_vllm_{uuid.uuid4().hex[:8]}"
    provider = f"priced-local-{uuid.uuid4().hex[:8]}"
    model = "local/priced-chat"
    endpoint = openai_compatible_chat_server["endpoint"]
    op = None
    try:
        rvbbit.execute(
            """
            SELECT rvbbit.register_backend(
              backend_name => %s,
              backend_endpoint => %s,
              backend_transport => 'openai_chat',
              backend_max_concur => 2,
              backend_timeout_ms => 5000,
              backend_opts => %s::jsonb)
            """,
            (name, endpoint, json.dumps({"model": model})),
        )
        doc = rvbbit.execute(
            """
            SELECT rvbbit.register_self_hosted_model(
              provider => %s,
              model => %s,
              backend_name => %s,
              input_per_mtok => 2.0,
              output_per_mtok => 4.0,
              cost_policy => 'model_rate')
            """,
            (provider, model, name),
        ).fetchone()[0]
        assert doc["cost_policy"] == "model_rate"
        rvbbit.execute("SELECT rvbbit.reload_backends()")
        op = _make_op(
            rvbbit,
            [
                {
                    "name": "ask",
                    "kind": "llm",
                    "provider": name,
                    "model": model,
                    "user": "price {{ inputs.text }}",
                    "max_tokens": 4,
                    "temperature": 0,
                }
            ],
        )
        query_id = rvbbit.execute("SELECT rvbbit.reset_query_id()").fetchone()[0]
        assert "price sample" in rvbbit.execute(f"SELECT rvbbit.{op}('sample')").fetchone()[0]
        rvbbit.execute("SELECT rvbbit.backfill_cost_events_from_receipts(1000)")
        cost = rvbbit.execute(
            """
            SELECT status, cost_source, round(cost_usd::numeric, 8)::text
            FROM rvbbit.cost_latest
            WHERE query_id = %s::uuid AND backend = %s
            ORDER BY event_id DESC
            LIMIT 1
            """,
            (str(query_id), name),
        ).fetchone()
        assert cost == ("estimated", "policy_model_rate", "0.00005000")
    finally:
        if op:
            _drop_op(rvbbit, op)
        rvbbit.execute("DELETE FROM rvbbit.backends WHERE name = %s", (name,))
        rvbbit.execute("SELECT rvbbit.reload_backends()")


def test_bogus_transport_rejected(rvbbit):
    """An unknown transport fails the catalog CHECK."""
    with pytest.raises(Exception):
        rvbbit.execute(
            "SELECT rvbbit.register_backend("
            "  backend_name => 'bad_xport', backend_endpoint => 'http://x', "
            "  backend_transport => 'not_a_transport')"
        )


def test_phase2_transports_reserved(rvbbit):
    """anthropic + gemini transports are accepted by the catalog (Phase 2)."""
    for xport in ("anthropic", "gemini"):
        name = f"p2_{xport}_{uuid.uuid4().hex[:6]}"
        try:
            rvbbit.execute(
                "SELECT rvbbit.register_backend("
                "  backend_name => %s, backend_endpoint => 'http://x', "
                "  backend_transport => %s)",
                (name, xport),
            )
        finally:
            rvbbit.execute(f"DELETE FROM rvbbit.backends WHERE name = '{name}'")


# ---- provider dispatch (deterministic — stub transport) ------------------


def test_llm_node_uses_pinned_provider(rvbbit, stub_provider):
    """An llm node's `provider` resolves + dispatches through the Transport."""
    op = _make_op(
        rvbbit,
        [
            {
                "name": "echo",
                "kind": "llm",
                "provider": stub_provider,
                "model": "stub-model",
                "user": "{{ inputs.text }}",
            }
        ],
    )
    try:
        # the stub provider echoes the rendered prompt — deterministic
        assert rvbbit.execute(f"SELECT rvbbit.{op}('hello world')").fetchone()[0] == "hello world"
        assert rvbbit.execute(f"SELECT rvbbit.{op}('hello world')").fetchone()[0] == "hello world"
    finally:
        _drop_op(rvbbit, op)


def test_llm_node_recorded_in_receipt(rvbbit, stub_provider):
    """The receipt's sub_call records the llm node."""
    op = _make_op(
        rvbbit,
        [
            {
                "name": "echo",
                "kind": "llm",
                "provider": stub_provider,
                "model": "stub-model",
                "user": "{{ inputs.text }}",
            }
        ],
    )
    try:
        rvbbit.execute(f"SELECT rvbbit.{op}('xyz')")
        kind = rvbbit.execute(
            "SELECT sub_calls->0->>'kind' FROM rvbbit.receipts "
            "WHERE operator = %s ORDER BY invocation_at DESC LIMIT 1",
            (op,),
        ).fetchone()[0]
        assert kind == "llm"
    finally:
        _drop_op(rvbbit, op)


def test_llm_node_warm_path(rvbbit, stub_provider):
    """prewarm pre-loads the provider backend so pool threads can dispatch."""
    op = _make_op(
        rvbbit,
        [
            {
                "name": "echo",
                "kind": "llm",
                "provider": stub_provider,
                "model": "stub-model",
                "user": "{{ inputs.text }}",
            }
        ],
    )
    try:
        res = rvbbit.execute(
            "SELECT n_inputs, n_executed FROM rvbbit.prewarm_operator(%s, %s)",
            (op, "SELECT unnest(ARRAY['a','b','c','d'])"),
        ).fetchone()
        assert res == (4, 4)
    finally:
        _drop_op(rvbbit, op)


def test_unknown_provider_fails_cleanly(rvbbit):
    """An llm node naming a missing provider fails cleanly — no value, the
    error captured in the receipt, the backend unharmed."""
    op = _make_op(
        rvbbit,
        [
            {
                "name": "echo",
                "kind": "llm",
                "provider": "no_such_provider_zzz",
                "model": "m",
                "user": "{{ inputs.text }}",
            }
        ],
    )
    try:
        out = rvbbit.execute(f"SELECT rvbbit.{op}('hi')").fetchone()[0]
        assert not out  # the step failed — no value produced
        err = rvbbit.execute(
            "SELECT error FROM rvbbit.receipts WHERE operator = %s "
            "ORDER BY invocation_at DESC LIMIT 1",
            (op,),
        ).fetchone()[0]
        assert err and "no_such_provider_zzz" in err
    finally:
        _drop_op(rvbbit, op)


# ---- Phase 2 transports are wired -----------------------------------------


def _transport_wired(rvbbit, transport, endpoint, model):
    """Register a provider on `transport` pointed at a dead endpoint, call
    it, and assert the failure is a transport error — not 'not implemented'.
    Proves the transport is registered and fails gracefully (no crash)."""
    name = f"wired_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(
        "SELECT rvbbit.register_backend(backend_name => %s, backend_endpoint => %s, "
        "backend_transport => %s, backend_timeout_ms => 3000)",
        (name, endpoint, transport),
    )
    op = _make_op(
        rvbbit,
        [
            {
                "name": "x",
                "kind": "llm",
                "provider": name,
                "model": model,
                "user": "{{ inputs.text }}",
            }
        ],
    )
    try:
        rvbbit.execute(f"SELECT rvbbit.{op}('hi')")
        err = rvbbit.execute(
            "SELECT error FROM rvbbit.receipts WHERE operator = %s "
            "ORDER BY invocation_at DESC LIMIT 1",
            (op,),
        ).fetchone()[0]
        assert err, "expected a transport-level error"
        assert "not implemented" not in err.lower(), f"transport not wired: {err}"
    finally:
        _drop_op(rvbbit, op)
        rvbbit.execute(f"DELETE FROM rvbbit.backends WHERE name = '{name}'")


def test_anthropic_transport_wired(rvbbit):
    """The anthropic transport is registered and runs (fails on connection)."""
    _transport_wired(
        rvbbit, "anthropic", "http://127.0.0.1:9999/v1/messages", "claude-haiku-4-5"
    )


def test_gemini_transport_wired(rvbbit):
    """The gemini transport is registered and runs (fails on connection)."""
    _transport_wired(
        rvbbit,
        "gemini",
        "http://127.0.0.1:9999/v1beta/models/{model}:generateContent",
        "gemini-2.5-flash",
    )


# ---- live (real OpenRouter) ---------------------------------------------


@LIVE
def test_default_provider_live(rvbbit):
    """A single-LLM operator with no provider routes to the default."""
    op = f"liveop_{uuid.uuid4().hex[:8]}"
    rvbbit.execute(
        "SELECT rvbbit.create_operator("
        "  op_name => %s, op_arg_names => ARRAY['text'], "
        "  op_return_type => 'text', "
        "  op_system => 'Reply with exactly one lowercase word.', "
        "  op_user => 'What color is {{ inputs.text }}?')",
        (op,),
    )
    try:
        out = rvbbit.execute(f"SELECT rvbbit.{op}('a clear daytime sky')").fetchone()[0]
        assert out and out.strip()
    finally:
        _drop_op(rvbbit, op)


@LIVE
def test_explicit_provider_live(rvbbit):
    """An llm node pinned to the openrouter provider makes a real call."""
    op = _make_op(
        rvbbit,
        [
            {
                "name": "ask",
                "kind": "llm",
                "provider": "openrouter",
                "model": "openai/gpt-5.4-mini",
                "system": "Reply with exactly one lowercase word.",
                "user": "What color is {{ inputs.text }}?",
            }
        ],
    )
    try:
        out = rvbbit.execute(f"SELECT rvbbit.{op}('fresh grass')").fetchone()[0]
        assert out and out.strip()
    finally:
        _drop_op(rvbbit, op)


@LIVE
def test_openai_direct_provider_live_estimated_cost(rvbbit):
    """OpenAI direct uses openai_chat and model-rate cost estimates."""
    backend = f"openai_direct_{uuid.uuid4().hex[:8]}"
    model = os.environ.get("RUN_OPENAI_MODEL", "gpt-5.4-mini")
    rvbbit.execute(
        """
        SELECT rvbbit.register_backend(
          backend_name => %s,
          backend_endpoint => 'https://api.openai.com/v1/chat/completions',
          backend_transport => 'openai_chat',
          backend_max_concur => 2,
          backend_timeout_ms => 120000,
          backend_auth_env => 'OPENAI_API_KEY',
          backend_opts => '{"max_tokens_field":"max_completion_tokens"}'::jsonb)
        """,
        (backend,),
    )
    op = _make_op(
        rvbbit,
        [
            {
                "name": "ask",
                "kind": "llm",
                "provider": backend,
                "model": model,
                "system": "Reply with exactly one lowercase word.",
                "user": "What color is {{ inputs.text }}?",
                "max_tokens": 16,
                "temperature": 0,
            }
        ],
    )
    try:
        assert rvbbit.execute(
            "SELECT 1 FROM rvbbit.model_rates WHERE model = %s", (model,)
        ).fetchone()
        rvbbit.execute("SELECT rvbbit.reload_backends()")
        query_id = rvbbit.execute("SELECT rvbbit.reset_query_id()").fetchone()[0]
        out = rvbbit.execute(f"SELECT rvbbit.{op}('fresh grass')").fetchone()[0]
        assert out and out.strip()
        receipt = rvbbit.execute(
            """
            SELECT error, sub_calls->0->>'backend', sub_calls->0->>'transport',
                   (sub_calls->0->>'tokens_in')::int,
                   (sub_calls->0->>'tokens_out')::int
            FROM rvbbit.receipts
            WHERE operator = %s AND query_id = %s::uuid
            ORDER BY invocation_at DESC
            LIMIT 1
            """,
            (op, str(query_id)),
        ).fetchone()
        assert receipt and receipt[0] is None
        assert receipt[1] == backend
        assert receipt[2] == "openai_chat"
        assert receipt[3] > 0 and receipt[4] > 0
        rvbbit.execute("SELECT rvbbit.backfill_cost_events_from_receipts(1000)")
        cost = rvbbit.execute(
            """
            SELECT status, cost_source, cost_usd::float8
            FROM rvbbit.cost_latest
            WHERE query_id = %s::uuid AND backend = %s
            ORDER BY event_id DESC
            LIMIT 1
            """,
            (str(query_id), backend),
        ).fetchone()
        assert cost and cost[0] == "estimated"
        assert cost[1] == "model_rate"
        assert cost[2] > 0
    finally:
        _drop_op(rvbbit, op)
        rvbbit.execute("DELETE FROM rvbbit.backends WHERE name = %s", (backend,))
        rvbbit.execute("SELECT rvbbit.reload_backends()")


@LIVE
def test_anthropic_direct_provider_live_estimated_cost(rvbbit):
    """Anthropic direct uses the Messages transport and model-rate costs."""
    backend = f"anthropic_direct_{uuid.uuid4().hex[:8]}"
    model = os.environ.get("RUN_ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
    rvbbit.execute(
        """
        SELECT rvbbit.register_backend(
          backend_name => %s,
          backend_endpoint => 'https://api.anthropic.com/v1/messages',
          backend_transport => 'anthropic',
          backend_max_concur => 2,
          backend_timeout_ms => 120000,
          backend_auth_env => 'ANTHROPIC_API_KEY')
        """,
        (backend,),
    )
    op = _make_op(
        rvbbit,
        [
            {
                "name": "ask",
                "kind": "llm",
                "provider": backend,
                "model": model,
                "system": "Reply with exactly one lowercase word.",
                "user": "What color is {{ inputs.text }}?",
                "max_tokens": 16,
                "temperature": 0,
            }
        ],
    )
    try:
        assert rvbbit.execute(
            "SELECT 1 FROM rvbbit.model_rates WHERE model = %s", (model,)
        ).fetchone()
        rvbbit.execute("SELECT rvbbit.reload_backends()")
        query_id = rvbbit.execute("SELECT rvbbit.reset_query_id()").fetchone()[0]
        out = rvbbit.execute(f"SELECT rvbbit.{op}('fresh grass')").fetchone()[0]
        assert out and out.strip()
        receipt = rvbbit.execute(
            """
            SELECT error, sub_calls->0->>'backend', sub_calls->0->>'transport',
                   (sub_calls->0->>'tokens_in')::int,
                   (sub_calls->0->>'tokens_out')::int
            FROM rvbbit.receipts
            WHERE operator = %s AND query_id = %s::uuid
            ORDER BY invocation_at DESC
            LIMIT 1
            """,
            (op, str(query_id)),
        ).fetchone()
        assert receipt and receipt[0] is None
        assert receipt[1] == backend
        assert receipt[2] == "anthropic"
        assert receipt[3] > 0 and receipt[4] > 0
        rvbbit.execute("SELECT rvbbit.backfill_cost_events_from_receipts(1000)")
        cost = rvbbit.execute(
            """
            SELECT status, cost_source, cost_usd::float8
            FROM rvbbit.cost_latest
            WHERE query_id = %s::uuid AND backend = %s
            ORDER BY event_id DESC
            LIMIT 1
            """,
            (str(query_id), backend),
        ).fetchone()
        assert cost and cost[0] == "estimated"
        assert cost[1] == "model_rate"
        assert cost[2] > 0
    finally:
        _drop_op(rvbbit, op)
        rvbbit.execute("DELETE FROM rvbbit.backends WHERE name = %s", (backend,))
        rvbbit.execute("SELECT rvbbit.reload_backends()")


@LIVE
def test_gemini_api_key_provider_live_estimated_cost(rvbbit):
    """Gemini API-key mode uses x-goog-api-key and model-rate costs."""
    if not os.environ.get("GEMINI_API_KEY"):
        pytest.skip("GEMINI_API_KEY is not set")
    backend = f"gemini_key_{uuid.uuid4().hex[:8]}"
    model = os.environ.get("RUN_GEMINI_MODEL", "gemini-2.5-flash-lite")
    rvbbit.execute(
        """
        SELECT rvbbit.register_backend(
          backend_name => %s,
          backend_endpoint => 'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent',
          backend_transport => 'gemini',
          backend_max_concur => 2,
          backend_timeout_ms => 120000,
          backend_auth_env => 'GEMINI_API_KEY')
        """,
        (backend,),
    )
    op = _make_op(
        rvbbit,
        [
            {
                "name": "ask",
                "kind": "llm",
                "provider": backend,
                "model": model,
                "system": "Reply with exactly one lowercase word.",
                "user": "What color is {{ inputs.text }}?",
                "max_tokens": 16,
                "temperature": 0,
            }
        ],
    )
    try:
        assert rvbbit.execute(
            "SELECT 1 FROM rvbbit.model_rates WHERE model = %s", (model,)
        ).fetchone()
        rvbbit.execute("SELECT rvbbit.reload_backends()")
        query_id = rvbbit.execute("SELECT rvbbit.reset_query_id()").fetchone()[0]
        out = rvbbit.execute(f"SELECT rvbbit.{op}('fresh grass')").fetchone()[0]
        assert out and out.strip()
        receipt = rvbbit.execute(
            """
            SELECT error, sub_calls->0->>'backend', sub_calls->0->>'transport',
                   (sub_calls->0->>'tokens_in')::int,
                   (sub_calls->0->>'tokens_out')::int
            FROM rvbbit.receipts
            WHERE operator = %s AND query_id = %s::uuid
            ORDER BY invocation_at DESC
            LIMIT 1
            """,
            (op, str(query_id)),
        ).fetchone()
        assert receipt and receipt[0] is None
        assert receipt[1] == backend
        assert receipt[2] == "gemini"
        assert receipt[3] > 0 and receipt[4] > 0
        rvbbit.execute("SELECT rvbbit.backfill_cost_events_from_receipts(1000)")
        cost = rvbbit.execute(
            """
            SELECT status, cost_source, cost_usd::float8
            FROM rvbbit.cost_latest
            WHERE query_id = %s::uuid AND backend = %s
            ORDER BY event_id DESC
            LIMIT 1
            """,
            (str(query_id), backend),
        ).fetchone()
        assert cost and cost[0] == "estimated"
        assert cost[1] == "model_rate"
        assert cost[2] > 0
    finally:
        _drop_op(rvbbit, op)
        rvbbit.execute("DELETE FROM rvbbit.backends WHERE name = %s", (backend,))
        rvbbit.execute("SELECT rvbbit.reload_backends()")


@LIVE
def test_gemini_adc_provider_live_estimated_cost(rvbbit):
    """Gemini ADC mode mints a Google OAuth token and estimates model cost."""
    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        pytest.skip("GOOGLE_APPLICATION_CREDENTIALS is not set")
    backend = f"gemini_adc_{uuid.uuid4().hex[:8]}"
    model = os.environ.get("RUN_GEMINI_MODEL", "gemini-2.5-flash-lite")
    rvbbit.execute(
        """
        SELECT rvbbit.register_backend(
          backend_name => %s,
          backend_endpoint => 'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent',
          backend_transport => 'gemini',
          backend_max_concur => 2,
          backend_timeout_ms => 120000,
          backend_auth_env => 'GOOGLE_APPLICATION_CREDENTIALS',
          backend_opts => '{"auth_mode":"google_adc"}'::jsonb)
        """,
        (backend,),
    )
    op = _make_op(
        rvbbit,
        [
            {
                "name": "ask",
                "kind": "llm",
                "provider": backend,
                "model": model,
                "system": "Reply with exactly one lowercase word.",
                "user": "What color is {{ inputs.text }}?",
                "max_tokens": 16,
                "temperature": 0,
            }
        ],
    )
    try:
        assert rvbbit.execute(
            "SELECT 1 FROM rvbbit.model_rates WHERE model = %s", (model,)
        ).fetchone()
        rvbbit.execute("SELECT rvbbit.reload_backends()")
        query_id = rvbbit.execute("SELECT rvbbit.reset_query_id()").fetchone()[0]
        out = rvbbit.execute(f"SELECT rvbbit.{op}('fresh grass')").fetchone()[0]
        assert out and out.strip()
        receipt = rvbbit.execute(
            """
            SELECT error, sub_calls->0->>'backend', sub_calls->0->>'transport',
                   (sub_calls->0->>'tokens_in')::int,
                   (sub_calls->0->>'tokens_out')::int
            FROM rvbbit.receipts
            WHERE operator = %s AND query_id = %s::uuid
            ORDER BY invocation_at DESC
            LIMIT 1
            """,
            (op, str(query_id)),
        ).fetchone()
        assert receipt and receipt[0] is None
        assert receipt[1] == backend
        assert receipt[2] == "gemini"
        assert receipt[3] > 0 and receipt[4] > 0
        rvbbit.execute("SELECT rvbbit.backfill_cost_events_from_receipts(1000)")
        cost = rvbbit.execute(
            """
            SELECT status, cost_source, cost_usd::float8
            FROM rvbbit.cost_latest
            WHERE query_id = %s::uuid AND backend = %s
            ORDER BY event_id DESC
            LIMIT 1
            """,
            (str(query_id), backend),
        ).fetchone()
        assert cost and cost[0] == "estimated"
        assert cost[1] == "model_rate"
        assert cost[2] > 0
    finally:
        _drop_op(rvbbit, op)
        rvbbit.execute("DELETE FROM rvbbit.backends WHERE name = %s", (backend,))
        rvbbit.execute("SELECT rvbbit.reload_backends()")
