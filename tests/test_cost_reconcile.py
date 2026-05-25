from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest


def _start_generation_server(payloads):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            generation_id = parse_qs(parsed.query).get("id", [""])[0]
            seen.append(
                {
                    "path": parsed.path,
                    "id": generation_id,
                    "authorization": self.headers.get("authorization"),
                }
            )
            payload = payloads.get(generation_id)
            if payload is None:
                self.send_response(404)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "not found"}).encode())
                return
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://bench:{server.server_port}/generation"
    return server, endpoint, seen


def _configure_openrouter_endpoint(rvbbit, endpoint):
    old = rvbbit.execute(
        "SELECT auth_header_env, transport_opts FROM rvbbit.backends WHERE name = 'openrouter'"
    ).fetchone()
    rvbbit.execute(
        """
        UPDATE rvbbit.backends
        SET auth_header_env = 'PATH',
            transport_opts = jsonb_set(
                coalesce(transport_opts, '{}'::jsonb),
                '{generation_endpoint}',
                to_jsonb(%s::text),
                true
            )
        WHERE name = 'openrouter'
        """,
        (endpoint,),
    )
    return old


def _restore_openrouter(rvbbit, old):
    if old is None:
        return
    rvbbit.execute(
        """
        UPDATE rvbbit.backends
        SET auth_header_env = %s,
            transport_opts = %s::jsonb
        WHERE name = 'openrouter'
        """,
        (old[0], json.dumps(old[1])),
    )


def _insert_pending_generation(rvbbit, generation_id, cost_source="provider_settled"):
    rvbbit.execute(
        "DELETE FROM rvbbit.cost_events WHERE provider_generation_id LIKE 'gen-%'"
    )
    cost_request_id = str(uuid.uuid4())
    query_id = str(uuid.uuid4())
    rvbbit.execute(
        """
        INSERT INTO rvbbit.cost_events
            (cost_request_id, query_id, source, backend, transport, model,
             provider_generation_id, status, cost_source, tokens_in, tokens_out, raw)
        VALUES
            (%s::uuid, %s::uuid, 'operator', 'openrouter', 'openai_chat',
             'openai/gpt-5.4-mini', %s, 'pending', %s, 11, 7, '{}'::jsonb)
        """,
        (cost_request_id, query_id, generation_id, cost_source),
    )
    return cost_request_id, query_id


@pytest.mark.parametrize("initial_cost_source", ["provider_settled", "openrouter_generation"])
def test_openrouter_reconcile_settles_pending_generation(rvbbit, initial_cost_source):
    generation_id = f"gen-{uuid.uuid4().hex}"
    server, endpoint, seen = _start_generation_server(
        {
            generation_id: {
                "data": {
                    "id": generation_id,
                    "model": "openai/gpt-5.4-mini",
                    "total_cost": 0.0015,
                    "tokens_prompt": 33,
                    "tokens_completion": 12,
                    "native_tokens_prompt": 40,
                    "native_tokens_completion": 14,
                    "native_tokens_reasoning": 3,
                    "native_tokens_cached": 5,
                    "request_id": "req-test",
                    "upstream_id": "upstream-test",
                }
            }
        }
    )
    old = _configure_openrouter_endpoint(rvbbit, endpoint)
    try:
        cost_request_id, query_id = _insert_pending_generation(
            rvbbit, generation_id, initial_cost_source
        )

        settled = rvbbit.execute("SELECT rvbbit.reconcile_openrouter_costs(10)").fetchone()[0]
        assert settled == 1
        assert seen and seen[0]["id"] == generation_id
        assert (seen[0]["authorization"] or "").startswith("Bearer ")

        row = rvbbit.execute(
            """
            SELECT status, cost_source, cost_usd::float8, query_id::text,
                   provider_request_id, upstream_id, tokens_in, tokens_out,
                   native_tokens_in, native_tokens_out, reasoning_tokens, cached_tokens
            FROM rvbbit.cost_latest
            WHERE cost_request_id = %s::uuid
            """,
            (cost_request_id,),
        ).fetchone()
        assert row == (
            "settled",
            "openrouter_generation",
            pytest.approx(0.0015),
            query_id,
            "req-test",
            "upstream-test",
            33,
            12,
            40,
            14,
            3,
            5,
        )
    finally:
        _restore_openrouter(rvbbit, old)
        server.shutdown()


def test_openrouter_reconcile_missing_cost_stays_pending(rvbbit):
    generation_id = f"gen-{uuid.uuid4().hex}"
    server, endpoint, _seen = _start_generation_server(
        {
            generation_id: {
                "data": {
                    "id": generation_id,
                    "model": "openai/gpt-5.4-mini",
                    "tokens_prompt": 33,
                    "tokens_completion": 12,
                }
            }
        }
    )
    old = _configure_openrouter_endpoint(rvbbit, endpoint)
    try:
        cost_request_id, _query_id = _insert_pending_generation(rvbbit, generation_id)

        settled = rvbbit.execute("SELECT rvbbit.reconcile_openrouter_costs(10)").fetchone()[0]
        assert settled == 0

        row = rvbbit.execute(
            """
            SELECT status, cost_source, cost_usd::float8, raw->>'error'
            FROM rvbbit.cost_latest
            WHERE cost_request_id = %s::uuid
            """,
            (cost_request_id,),
        ).fetchone()
        assert row[0] == "pending"
        assert row[1] == "openrouter_generation"
        assert row[2] is None
        assert "total_cost" in row[3]
    finally:
        _restore_openrouter(rvbbit, old)
        server.shutdown()
