#!/usr/bin/env python3
"""Rvbbit real-world acceptance harness.

This is intentionally not a pytest suite. It is a staged user journey runner
that logs enough context to debug a failed install: JSONL artifacts on disk and
SQL rows in rvbbit_e2e.* inside the target database.

Default mode is hermetic: deterministic sidecars and stub/local transports,
no paid provider calls. Live LLM checks are opt-in with RVBBIT_E2E_LIVE_LLM=1.
"""
from __future__ import annotations

import contextlib
import csv
import datetime as dt
import json
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable

import psycopg


RVBBIT_DSN = os.environ.get(
    "RVBBIT_DSN", "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench"
)
OUT_ROOT = Path(os.environ.get("RVBBIT_E2E_OUT_ROOT", "/results/e2e"))
LIVE_LLM = os.environ.get("RVBBIT_E2E_LIVE_LLM", "").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
LIVE_ROWS = max(1, int(os.environ.get("RVBBIT_E2E_LIVE_ROWS", "3")))
OPENAI_LIVE_MODEL = os.environ.get("RVBBIT_E2E_OPENAI_MODEL", "gpt-5.4-mini")
ANTHROPIC_LIVE_MODEL = os.environ.get(
    "RVBBIT_E2E_ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"
)
GEMINI_LIVE_MODEL = os.environ.get("RVBBIT_E2E_GEMINI_MODEL", "gemini-2.5-flash-lite")
BIGFOOT_ROWS = max(1, int(os.environ.get("RVBBIT_E2E_BIGFOOT_ROWS", "25")))
MODEL_TRAINING_ROWS = max(
    25, int(os.environ.get("RVBBIT_E2E_MODEL_TRAINING_ROWS", "250"))
)
SEMANTIC_STRESS_ROWS = max(
    1, int(os.environ.get("RVBBIT_E2E_SEMANTIC_STRESS_ROWS", "500"))
)
KEEP_OBJECTS = os.environ.get("RVBBIT_E2E_KEEP_OBJECTS", "").lower() in {
    "1",
    "true",
    "yes",
    "on",
}

ECHO_BASE = os.environ.get("RVBBIT_E2E_ECHO_BASE", "http://rvbbit-echo:8080")
ECHO_PREDICT = f"{ECHO_BASE}/predict"
BIGFOOT_CSV_CANDIDATES = [
    "/csv-files/bigfoot_sightings.csv",
    "/bench/bigfoot_sightings.csv",
    "/home/ryanr/csv-files/bigfoot_sightings.csv",
]
BIGFOOT_LOCATION_CSV_CANDIDATES = [
    "/csv-files/bigfoot_sightings_locations.csv",
    "/home/ryanr/csv-files/bigfoot_sightings_locations.csv",
]
RVBBIT_TRAINER = os.environ.get("RVBBIT_E2E_TRAINER", "/capabilities/tools/rvbbit-trainer")


def json_default(value: Any) -> str:
    return str(value)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def short_id() -> str:
    return uuid.uuid4().hex[:10]


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def sql_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("0.0.0.0", 0))
        return int(sock.getsockname()[1])


def http_json(url: str, *, method: str = "GET", timeout: float = 3.0) -> Any:
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    if not body:
        return None
    return json.loads(body.decode("utf-8"))


def service_alive(url: str) -> bool:
    try:
        urllib.request.urlopen(url, timeout=3).read()
        return True
    except Exception:
        return False


@contextlib.contextmanager
def local_openai_chat_server():
    seen: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - stdlib callback
            length = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8") or "{}")
            seen.append(
                {
                    "path": self.path,
                    "authorization": self.headers.get("authorization"),
                    "body": payload,
                }
            )
            model = payload.get("model") or ""
            user = ""
            for msg in payload.get("messages") or []:
                if msg.get("role") == "user":
                    user = msg.get("content") or ""
            body = {
                "id": f"chatcmpl-{short_id()}",
                "object": "chat.completion",
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": f"local-compatible-ok model={model} user={user}",
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

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {
            "endpoint": f"http://bench:{server.server_port}/v1/chat/completions",
            "seen": seen,
        }
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def run_command(args: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


class E2EHarness:
    def __init__(self) -> None:
        self.run_id = f"e2e_{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{short_id()}"
        self.mode = "live" if LIVE_LLM else "hermetic"
        self.artifact_dir = OUT_ROOT / self.run_id
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.artifact_dir / "events.jsonl"
        self.summary_path = self.artifact_dir / "summary.json"
        self.report_path = self.artifact_dir / "report.md"
        self.events: list[dict[str, Any]] = []
        self.seq = 0
        self.failures = 0
        self.skips = 0
        self.created_tables: list[str] = []
        self.created_schemas: list[str] = []
        self.created_backends: list[str] = []
        self.created_ops: list[str] = []
        self.created_mcp_servers: list[str] = []
        self.created_python_envs: list[str] = []
        self.created_python_handlers: list[str] = []
        self.created_route_profiles: list[str] = []
        self.created_stub_embeds: list[str] = []
        self.created_warren_nodes: list[str] = []
        self.created_warren_jobs: list[str] = []
        self.created_ml_models: list[str] = []
        self.child_processes: list[subprocess.Popen[Any]] = []

        self.conn = psycopg.connect(RVBBIT_DSN, autocommit=True)
        self.ensure_log_tables()
        self.conn.execute(
            """
            INSERT INTO rvbbit_e2e.runs
                (run_id, mode, started_at, status, artifact_dir, summary)
            VALUES (%s, %s, clock_timestamp(), 'running', %s, '{}'::jsonb)
            ON CONFLICT (run_id) DO NOTHING
            """,
            (self.run_id, self.mode, str(self.artifact_dir)),
        )

    def close(self) -> None:
        for proc in reversed(self.child_processes):
            if proc.poll() is None:
                with contextlib.suppress(Exception):
                    proc.terminate()
                with contextlib.suppress(Exception):
                    proc.wait(timeout=5)
                if proc.poll() is None:
                    with contextlib.suppress(Exception):
                        proc.kill()
        self.conn.close()

    def ensure_log_tables(self) -> None:
        self.conn.execute("CREATE SCHEMA IF NOT EXISTS rvbbit_e2e")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rvbbit_e2e.runs (
                run_id       text PRIMARY KEY,
                mode         text NOT NULL,
                started_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
                finished_at  timestamptz,
                status       text NOT NULL,
                artifact_dir text,
                summary      jsonb NOT NULL DEFAULT '{}'::jsonb
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rvbbit_e2e.events (
                run_id      text NOT NULL REFERENCES rvbbit_e2e.runs(run_id)
                            ON DELETE CASCADE,
                seq         integer NOT NULL,
                phase       text NOT NULL,
                step        text NOT NULL,
                status      text NOT NULL,
                started_at  timestamptz NOT NULL,
                duration_ms integer NOT NULL,
                details     jsonb NOT NULL DEFAULT '{}'::jsonb,
                error       text,
                PRIMARY KEY (run_id, seq)
            )
            """
        )

    def sql(self, sql: str, params: Iterable[Any] | None = None) -> list[tuple[Any, ...]]:
        cur = self.conn.execute(sql, params)
        if cur.description is None:
            return []
        return cur.fetchall()

    def scalar(self, sql: str, params: Iterable[Any] | None = None) -> Any:
        rows = self.sql(sql, params)
        return rows[0][0] if rows else None

    def log_event(
        self,
        *,
        phase: str,
        step: str,
        status: str,
        started_at: str,
        duration_ms: int,
        details: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        self.seq += 1
        event = {
            "run_id": self.run_id,
            "seq": self.seq,
            "phase": phase,
            "step": step,
            "status": status,
            "started_at": started_at,
            "duration_ms": duration_ms,
            "details": details or {},
            "error": error,
        }
        self.events.append(event)
        with self.jsonl_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, default=json_default, sort_keys=True) + "\n")
        self.conn.execute(
            """
            INSERT INTO rvbbit_e2e.events
                (run_id, seq, phase, step, status, started_at,
                 duration_ms, details, error)
            VALUES (%s, %s, %s, %s, %s, %s::timestamptz, %s, %s::jsonb, %s)
            """,
            (
                self.run_id,
                self.seq,
                phase,
                step,
                status,
                started_at,
                duration_ms,
                json.dumps(details or {}, default=json_default),
                error,
            ),
        )
        print(f"[{status.upper():<4}] {phase}/{step} ({duration_ms} ms)")
        if error:
            print(f"       {error.splitlines()[0]}")

    @contextlib.contextmanager
    def step(self, phase: str, step: str, *, optional: bool = False):
        started = now_iso()
        t0 = time.perf_counter()
        details: dict[str, Any] = {}
        try:
            yield details
        except SkipStep as exc:
            self.skips += 1
            elapsed = int((time.perf_counter() - t0) * 1000)
            self.log_event(
                phase=phase,
                step=step,
                status="skip",
                started_at=started,
                duration_ms=elapsed,
                details=details,
                error=str(exc),
            )
        except Exception as exc:  # keep going, but fail final status
            if not optional:
                self.failures += 1
            else:
                self.skips += 1
            elapsed = int((time.perf_counter() - t0) * 1000)
            error = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            details["traceback"] = traceback.format_exc(limit=8)
            self.log_event(
                phase=phase,
                step=step,
                status="skip" if optional else "fail",
                started_at=started,
                duration_ms=elapsed,
                details=details,
                error=error,
            )
        else:
            elapsed = int((time.perf_counter() - t0) * 1000)
            self.log_event(
                phase=phase,
                step=step,
                status="ok",
                started_at=started,
                duration_ms=elapsed,
                details=details,
            )

    def finish(self) -> int:
        if not KEEP_OBJECTS:
            self.cleanup()

        status = "failed" if self.failures else "passed"
        summary = {
            "run_id": self.run_id,
            "mode": self.mode,
            "status": status,
            "events": len(self.events),
            "failures": self.failures,
            "skips": self.skips,
            "artifact_dir": str(self.artifact_dir),
            "finished_at": now_iso(),
        }
        self.summary_path.write_text(
            json.dumps(summary, indent=2, default=json_default) + "\n",
            encoding="utf-8",
        )
        self.write_report(summary)
        self.conn.execute(
            """
            UPDATE rvbbit_e2e.runs
            SET finished_at = clock_timestamp(),
                status = %s,
                summary = %s::jsonb
            WHERE run_id = %s
            """,
            (status, json.dumps(summary), self.run_id),
        )
        print(f"\nreport: {self.report_path}")
        print(f"events: {self.jsonl_path}")
        return 1 if self.failures else 0

    def write_report(self, summary: dict[str, Any]) -> None:
        lines = [
            f"# Rvbbit Acceptance Run `{self.run_id}`",
            "",
            f"- status: `{summary['status']}`",
            f"- mode: `{self.mode}`",
            f"- failures: `{self.failures}`",
            f"- skips: `{self.skips}`",
            f"- events: `{len(self.events)}`",
            "",
            "## Events",
            "",
            "| # | Status | Phase | Step | ms | Notes |",
            "|---:|---|---|---|---:|---|",
        ]
        for ev in self.events:
            note = ev.get("error") or ""
            if not note and ev.get("details"):
                compact = json.dumps(ev["details"], default=json_default, sort_keys=True)
                note = compact[:180] + ("..." if len(compact) > 180 else "")
            note = note.replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| {ev['seq']} | `{ev['status']}` | {ev['phase']} | "
                f"{ev['step']} | {ev['duration_ms']} | {note} |"
            )
        lines.append("")
        self.report_path.write_text("\n".join(lines), encoding="utf-8")

    def cleanup(self) -> None:
        for server in reversed(self.created_mcp_servers):
            with contextlib.suppress(Exception):
                self.conn.execute("SELECT rvbbit.drop_mcp_server(%s)", (server,))
        for handler in reversed(self.created_python_handlers):
            with contextlib.suppress(Exception):
                self.conn.execute("DELETE FROM rvbbit.python_handlers WHERE name = %s", (handler,))
        for env in reversed(self.created_python_envs):
            with contextlib.suppress(Exception):
                self.conn.execute("DELETE FROM rvbbit.python_envs WHERE name = %s", (env,))
        if self.created_python_envs or self.created_python_handlers:
            with contextlib.suppress(Exception):
                self.conn.execute("SELECT rvbbit.reload_python_runtime()")
        for table in reversed(self.created_tables):
            with contextlib.suppress(Exception):
                self.conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        for schema in reversed(self.created_schemas):
            with contextlib.suppress(Exception):
                self.conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        for specialist in reversed(self.created_stub_embeds):
            with contextlib.suppress(Exception):
                self.conn.execute("SELECT rvbbit.embedding_purge(%s)", (specialist,))
        for job_id in reversed(self.created_warren_jobs):
            with contextlib.suppress(Exception):
                self.conn.execute(
                    "DELETE FROM rvbbit.warren_deployments WHERE job_id = %s::uuid",
                    (job_id,),
                )
            with contextlib.suppress(Exception):
                self.conn.execute(
                    "DELETE FROM rvbbit.warren_jobs WHERE job_id = %s::uuid",
                    (job_id,),
                )
        for node in reversed(self.created_warren_nodes):
            with contextlib.suppress(Exception):
                self.conn.execute(
                    "DELETE FROM rvbbit.warren_node_metrics WHERE node_name = %s",
                    (node,),
                )
            with contextlib.suppress(Exception):
                self.conn.execute(
                    "DELETE FROM rvbbit.warren_deployments WHERE node_name = %s",
                    (node,),
                )
            with contextlib.suppress(Exception):
                self.conn.execute("DELETE FROM rvbbit.warren_nodes WHERE name = %s", (node,))
        for profile in reversed(self.created_route_profiles):
            with contextlib.suppress(Exception):
                self.conn.execute(
                    "DELETE FROM rvbbit.route_profiles WHERE name = %s",
                    (profile,),
                )
        for op in reversed(self.created_ops):
            with contextlib.suppress(Exception):
                rows = self.sql(
                    """
                    SELECT pg_get_function_identity_arguments(p.oid)
                    FROM pg_proc p
                    WHERE p.pronamespace = 'rvbbit'::regnamespace
                      AND p.proname = %s
                    """,
                    (op,),
                )
                for (identity_args,) in rows:
                    self.conn.execute(
                        f"DROP FUNCTION IF EXISTS rvbbit.{sql_ident(op)}({identity_args})"
                    )
            with contextlib.suppress(Exception):
                self.conn.execute("DELETE FROM rvbbit.operators WHERE name = %s", (op,))
            with contextlib.suppress(Exception):
                self.conn.execute(f"DROP FUNCTION IF EXISTS rvbbit.{op}(text, jsonb)")
            with contextlib.suppress(Exception):
                self.conn.execute(f"DROP FUNCTION IF EXISTS rvbbit.{op}(jsonb, jsonb)")
            with contextlib.suppress(Exception):
                self.conn.execute(f"DROP FUNCTION IF EXISTS rvbbit.{op}(jsonb)")
        for model_name in getattr(self, "created_ml_models", []):
            with contextlib.suppress(Exception):
                self.conn.execute("DELETE FROM rvbbit.ml_models WHERE name = %s", (model_name,))
        for backend in reversed(self.created_backends):
            with contextlib.suppress(Exception):
                self.conn.execute("DELETE FROM rvbbit.backends WHERE name = %s", (backend,))
        if self.created_backends:
            with contextlib.suppress(Exception):
                self.conn.execute("SELECT rvbbit.reload_backends()")


class SkipStep(Exception):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def phase_catalog(h: E2EHarness) -> None:
    with h.step("catalog", "extension_and_core_objects") as d:
        d["version"] = h.scalar("SELECT extversion FROM pg_extension WHERE extname = 'pg_rvbbit'")
        require(d["version"] is not None, "pg_rvbbit extension is not installed")
        required_relations = [
            "rvbbit.operators",
            "rvbbit.backends",
            "rvbbit.receipts",
            "rvbbit.cost_events",
            "rvbbit.provider_catalog",
            "rvbbit.provider_models",
            "rvbbit.model_rate_cards",
            "rvbbit.kg_nodes",
            "rvbbit.mcp_servers",
            "rvbbit.route_profiles",
            "rvbbit.warren_nodes",
        ]
        rows = [
            (name, h.scalar(f"SELECT to_regclass({sql_literal(name)}) IS NOT NULL"))
            for name in sorted(required_relations)
        ]
        d["relations"] = {name: present for name, present in rows}
        require(all(present for _, present in rows), f"missing relations: {rows}")
        d["operators"] = h.scalar("SELECT count(*) FROM rvbbit.operators")
        d["backends"] = h.scalar("SELECT count(*) FROM rvbbit.backends")

    with h.step("catalog", "observability_views") as d:
        views = [
            "rvbbit.query_costs",
            "rvbbit.provider_model_catalog",
            "rvbbit.receipt_cost_audit",
            "rvbbit.route_decision_summary",
            "rvbbit.warren_inventory",
        ]
        rows = [
            (name, h.scalar(f"SELECT to_regclass({sql_literal(name)}) IS NOT NULL"))
            for name in sorted(views)
        ]
        d["views"] = {name: present for name, present in rows}
        require(all(present for _, present in rows), f"missing observability views: {rows}")


def phase_provider_catalogs(h: E2EHarness) -> None:
    with h.step("catalog", "provider_catalog_refresh") as d:
        rows = h.sql(
            """
            SELECT provider, status, models, rates, error, auth_state
            FROM rvbbit.refresh_provider_catalogs('openrouter,openai,anthropic,gemini')
            ORDER BY provider
            """
        )
        d["refresh"] = rows
        require(rows, "provider catalog refresh returned no rows")
        require(
            all(row[1] in {"ok", "skipped", "error"} for row in rows),
            f"unexpected provider catalog statuses: {rows}",
        )
        d["summary"] = h.scalar("SELECT rvbbit.provider_catalog_summary()")
        require(isinstance(d["summary"], dict), f"bad provider summary: {d['summary']}")
        d["models"] = h.scalar("SELECT count(*) FROM rvbbit.provider_models")
        d["rate_cards"] = h.scalar("SELECT count(*) FROM rvbbit.model_rate_cards")
        d["maintain_no_storage"] = h.scalar(
            "SELECT rvbbit.maintain(refresh_catalogs => false, storage_tables => 0)"
        )
        require(isinstance(d["maintain_no_storage"], dict), "rvbbit.maintain did not return json")

    with h.step("catalog", "maintenance_scheduler_optional", optional=True) as d:
        status = h.scalar("SELECT rvbbit.install_maintenance_jobs(storage_tables => 0)")
        d["status"] = status
        require(isinstance(status, dict), f"bad maintenance scheduler status: {status}")


def phase_storage_and_routing(h: E2EHarness) -> str:
    suffix = short_id()
    table = f"public.e2e_storage_{suffix}"
    h.created_tables.append(table)
    with h.step("storage", "rvbbit_table_mutate_compact") as d:
        h.sql(f"CREATE TABLE {table} (id int PRIMARY KEY, body text, score int) USING rvbbit")
        h.sql(
            f"""
            INSERT INTO {table}
            SELECT g, 'body ' || g, g * 10
            FROM generate_series(1, 24) AS g
            """
        )
        h.sql(f"UPDATE {table} SET score = 999 WHERE id = 2")
        h.sql(f"DELETE FROM {table} WHERE id = 3")
        before = h.sql(f"SELECT count(*), sum(score) FROM {table}")[0]
        d["before_compact"] = {"rows": before[0], "sum_score": before[1]}
        require(before[0] == 23, f"expected 23 rows, got {before}")
        compacted = h.scalar(f"SELECT rvbbit.export_to_parquet('{table}'::regclass)")
        d["export_to_parquet"] = compacted
        after = h.sql(f"SELECT count(*), sum(score) FROM {table}")[0]
        d["after_compact"] = {"rows": after[0], "sum_score": after[1]}
        require(after == before, f"row count/sum changed after compact: {before} -> {after}")
        d["row_groups"] = h.scalar(
            "SELECT count(*) FROM rvbbit.row_groups WHERE table_oid = %s::regclass",
            (table,),
        )

    with h.step("routing", "route_status_and_explain") as d:
        d["status"] = h.scalar("SELECT rvbbit.route_status()")
        explanation = h.scalar(f"SELECT rvbbit.route_explain('SELECT sum(score) FROM {table}')")
        d["explain"] = explanation
        require(isinstance(explanation, dict), "route_explain did not return json")
        d["sum_score"] = h.scalar(f"SELECT sum(score) FROM {table}")
    return table


def phase_reload_persistence(h: E2EHarness, storage_table: str) -> None:
    suffix = short_id()
    graph = f"e2e_reload_graph_{suffix}"
    _, op = register_echo_operator(h, f"reload_{suffix}", batch_size=4)
    with h.step("persistence", "extension_reload_preserves_state") as d:
        if not service_alive(f"{ECHO_BASE}/health"):
            raise SkipStep(f"echo sidecar not reachable at {ECHO_BASE}")

        before = h.sql(f"SELECT count(*), sum(score) FROM {storage_table}")[0]
        d["table_before"] = {"rows": before[0], "sum_score": before[1]}
        op_before = h.scalar(f"SELECT rvbbit.{op}('reload smoke')")
        d["operator_before"] = op_before
        require(op_before == "RELOAD SMOKE", f"operator failed before reload: {op_before}")

        query_id = h.scalar("SELECT rvbbit.reset_query_id()")
        edge_id = h.scalar(
            """
            SELECT rvbbit.kg_assert_edge(
              %s, %s, 'survives_reload', %s, %s, 0.99,
              jsonb_build_object('text', 'reload persistence evidence',
                                 'query_id', %s::text),
              jsonb_build_object('run_id', %s::text),
              '', 0.0, %s
            )
            """,
            (
                f"e2e_reload_subject_{suffix}",
                "reload source",
                f"e2e_reload_object_{suffix}",
                "reload target",
                str(query_id),
                h.run_id,
                graph,
            ),
        )
        d["edge_id"] = edge_id
        d["kg_query_id"] = str(query_id)

        h.sql("ALTER EXTENSION pg_rvbbit UPDATE")
        h.sql("SELECT rvbbit.reload_backends()")

        after = h.sql(f"SELECT count(*), sum(score) FROM {storage_table}")[0]
        d["table_after"] = {"rows": after[0], "sum_score": after[1]}
        require(after == before, f"table changed across extension reload: {before} -> {after}")

        op_after = h.scalar(f"SELECT rvbbit.{op}('reload smoke')")
        d["operator_after"] = op_after
        require(op_after == "RELOAD SMOKE", f"operator failed after reload: {op_after}")

        evidence = h.sql(
            """
            SELECT query_id, evidence_text
            FROM rvbbit.kg_evidence
            WHERE graph_id = %s
            ORDER BY evidence_id DESC
            LIMIT 1
            """,
            (graph,),
        )
        d["kg_evidence"] = [
            {"query_id": str(row[0]), "evidence_text": row[1]} for row in evidence
        ]
        require(evidence and str(evidence[0][0]) == str(query_id), f"KG evidence lost across reload: {evidence}")


def phase_dump_restore(h: E2EHarness) -> None:
    suffix = short_id()
    schema = f"e2e_dump_{suffix}"
    table = f"{schema}.items"
    dump_path = h.artifact_dir / f"{schema}.sql"
    h.created_schemas.append(schema)
    h.created_tables.append(table)
    with h.step("persistence", "pg_dump_restore_recreates_rvbbit_table") as d:
        h.sql(f"CREATE SCHEMA {schema}")
        h.sql(
            f"""
            CREATE TABLE {table} (
              id int PRIMARY KEY,
              label text,
              score int
            ) USING rvbbit
            """
        )
        h.sql(
            f"""
            INSERT INTO {table}
            SELECT g, format('dump restore row %s', g), g * 7
            FROM generate_series(1, 64) AS g
            """
        )
        h.sql(f"SELECT rvbbit.export_to_parquet('{table}'::regclass)")
        before = h.sql(f"SELECT count(*), sum(score) FROM {table}")[0]
        d["before"] = {"rows": before[0], "sum_score": before[1]}

        dump = run_command(
            [
                "pg_dump",
                "--no-owner",
                "--no-privileges",
                "--table",
                table,
                "--file",
                str(dump_path),
                RVBBIT_DSN,
            ],
            timeout=120,
        )
        d["dump_path"] = str(dump_path)
        d["pg_dump"] = {
            "returncode": dump.returncode,
            "stderr": dump.stderr[-1000:],
        }
        require(dump.returncode == 0, f"pg_dump failed: {dump.stderr}")
        require(dump_path.exists() and dump_path.stat().st_size > 0, "pg_dump did not create artifact")
        d["dump_bytes"] = dump_path.stat().st_size

        h.sql(f"DROP TABLE {table} CASCADE")
        d["exists_after_drop"] = h.scalar(f"SELECT to_regclass({sql_literal(table)}) IS NOT NULL")
        require(d["exists_after_drop"] is False, f"{table} still exists after drop")

        restore = run_command(
            ["psql", RVBBIT_DSN, "-v", "ON_ERROR_STOP=1", "-f", str(dump_path)],
            timeout=120,
        )
        d["psql_restore"] = {
            "returncode": restore.returncode,
            "stdout": restore.stdout[-1000:],
            "stderr": restore.stderr[-1000:],
        }
        require(restore.returncode == 0, f"psql restore failed: {restore.stderr}")
        d["exists_after_restore"] = h.scalar(f"SELECT to_regclass({sql_literal(table)}) IS NOT NULL")
        require(d["exists_after_restore"] is True, f"{table} was not restored")

        after = h.sql(f"SELECT count(*), sum(score) FROM {table}")[0]
        d["after"] = {"rows": after[0], "sum_score": after[1]}
        require(after == before, f"restored table changed data: {before} -> {after}")
        d["exported_after_restore"] = h.scalar(f"SELECT rvbbit.export_to_parquet('{table}'::regclass)")
        d["row_groups_after_restore"] = h.scalar(
            "SELECT count(*) FROM rvbbit.row_groups WHERE table_oid = %s::regclass",
            (table,),
        )
        require(d["row_groups_after_restore"] >= 1, f"restored table did not rebuild parquet metadata: {d}")


def phase_route_training(h: E2EHarness) -> None:
    suffix = short_id()
    table = f"public.e2e_route_{suffix}"
    profile = f"e2e_route_profile_{suffix}"
    source = f"e2e:{h.run_id}"
    h.created_tables.append(table)
    h.created_route_profiles.append(profile)
    with h.step("routing", "train_profile_from_observations") as d:
        old_active = h.scalar("SELECT name FROM rvbbit.route_profiles WHERE active LIMIT 1")
        d["old_active_profile"] = old_active
        try:
            h.sql(f"CREATE TABLE {table} (id int PRIMARY KEY, score int, label text) USING rvbbit")
            h.sql(
                f"""
                INSERT INTO {table}
                SELECT g, g * 3, CASE WHEN g % 2 = 0 THEN 'even' ELSE 'odd' END
                FROM generate_series(1, 200) AS g
                """
            )
            h.sql(f"ANALYZE {table}")
            h.sql(f"SELECT rvbbit.export_to_parquet('{table}'::regclass)")
            query = f"SELECT sum(score), count(*) FROM {table} WHERE id BETWEEN 10 AND 180"
            d["query"] = query

            recorded = [
                h.scalar(
                    "SELECT rvbbit.route_record_observation(%s, %s, %s, 'ok', %s)",
                    (query, "rvbbit_native", 80.0, source),
                ),
                h.scalar(
                    "SELECT rvbbit.route_record_observation(%s, %s, %s, 'ok', %s)",
                    (query, "duck_vector", 8.0, source),
                ),
                h.scalar(
                    "SELECT rvbbit.route_record_observation(%s, %s, %s, 'ok', %s)",
                    (query, "datafusion_vector", 11.0, source),
                ),
            ]
            d["recorded_observations"] = recorded
            d["observation_count"] = h.scalar(
                "SELECT count(*) FROM rvbbit.route_observations WHERE source = %s",
                (source,),
            )
            require(d["observation_count"] == 3, f"expected 3 route observations: {d}")

            trained = h.scalar("SELECT rvbbit.route_train(%s, 1, 0.0)", (profile,))
            d["trained"] = trained
            require(trained["active"] is True, f"route_train did not activate profile: {trained}")
            require(trained["entries"] >= 1, f"route_train produced no entries: {trained}")

            h.sql("SELECT rvbbit.route_use_profile(%s, false)", (profile,))
            explained = h.scalar("SELECT rvbbit.route_explain(%s)", (query,))
            d["explain"] = {
                "profile_name": explained.get("profile_name"),
                "profile_source": explained.get("profile_source"),
                "chosen_candidate": explained.get("chosen_candidate"),
                "route_source": explained.get("route_source"),
                "reason": explained.get("reason"),
            }
            require(explained.get("profile_name") == profile, f"profile override not used: {explained}")
            require(explained.get("profile_source") == "guc", f"profile source not guc: {explained}")

            result = h.sql(query)
            d["query_result"] = result
            require(result and result[0][1] == 171, f"route training query returned wrong result: {result}")

            d["eval"] = h.scalar("SELECT rvbbit.route_eval(%s)", (profile,))
            require(d["eval"]["entries"] >= 1, f"route_eval saw no entries: {d['eval']}")
        finally:
            with contextlib.suppress(Exception):
                h.sql("SELECT rvbbit.route_clear_profile(false)")
            with contextlib.suppress(Exception):
                h.sql("UPDATE rvbbit.route_profiles SET active = false WHERE name = %s", (profile,))
            if old_active:
                with contextlib.suppress(Exception):
                    h.sql("UPDATE rvbbit.route_profiles SET active = true WHERE name = %s", (old_active,))


def phase_imports(h: E2EHarness) -> dict[str, str | None]:
    suffix = short_id()
    table = f"public.e2e_copy_import_{suffix}"
    imported: dict[str, str | None] = {
        "copy_table": table,
        "bigfoot_table": None,
        "bigfoot_full_table": None,
    }
    h.created_tables.append(table)
    with h.step("imports", "copy_into_rvbbit_table") as d:
        h.sql(
            f"""
            CREATE TABLE {table} (
              id int PRIMARY KEY,
              title text,
              state text,
              observed text
            ) USING rvbbit
            """
        )
        rows = [
            (1, "late shipment", "CO", "Customer reported late shipment and requested refund."),
            (2, "renewal risk", "CA", "Customer delayed renewal because support response was slow."),
            (3, "upgrade interest", "NY", "Analytics team asked about upgrading after successful pilot."),
            (4, "damaged package", "WA", "Retail team saw damaged packaging and requested replacement."),
            (5, "missing invoice", "TX", "Finance team escalated missing invoice and payment issue."),
        ]
        with h.conn.cursor().copy(
            f"COPY {table} (id, title, state, observed) FROM STDIN"
        ) as cp:
            for row in rows:
                cp.write_row(row)
        h.sql(f"ANALYZE {table}")
        d["rows"] = h.scalar(f"SELECT count(*) FROM {table}")
        require(d["rows"] == len(rows), f"COPY row count mismatch: {d}")
        d["exported"] = h.scalar(f"SELECT rvbbit.export_to_parquet('{table}'::regclass)")
        d["state_counts"] = h.sql(
            f"""
            SELECT state, count(*)
            FROM {table}
            GROUP BY state
            ORDER BY state
            """
        )
        require(len(d["state_counts"]) == 5, f"unexpected copied state counts: {d}")

    csv_path = next((Path(p) for p in BIGFOOT_CSV_CANDIDATES if Path(p).exists()), None)
    with h.step("imports", "optional_bigfoot_csv_sample", optional=True) as d:
        if csv_path is None:
            raise SkipStep(f"bigfoot CSV not found; tried {BIGFOOT_CSV_CANDIDATES}")
        bigfoot_table = f"public.e2e_bigfoot_{suffix}"
        imported["bigfoot_table"] = bigfoot_table
        h.created_tables.append(bigfoot_table)
        h.sql(
            f"""
            CREATE TABLE {bigfoot_table} (
              bfroid text PRIMARY KEY,
              title text,
              state text,
              county text,
              observed text
            ) USING rvbbit
            """
        )
        copied = 0
        with csv_path.open(newline="", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            with h.conn.cursor().copy(
                f"COPY {bigfoot_table} (bfroid, title, state, county, observed) FROM STDIN"
            ) as cp:
                for row in reader:
                    cp.write_row(
                        (
                            row.get("bfroid") or row.get("BfroId") or str(copied + 1),
                            row.get("title") or "",
                            row.get("state") or "",
                            row.get("county") or "",
                            (row.get("observed") or "")[:8000],
                        )
                    )
                    copied += 1
                    if copied >= BIGFOOT_ROWS:
                        break
        h.sql(f"ANALYZE {bigfoot_table}")
        d["csv_path"] = str(csv_path)
        d["rows"] = h.scalar(f"SELECT count(*) FROM {bigfoot_table}")
        require(d["rows"] == copied and copied > 0, f"bigfoot sample copy failed: {d}")
        d["exported"] = h.scalar(f"SELECT rvbbit.export_to_parquet('{bigfoot_table}'::regclass)")
        d["top_states"] = h.sql(
            f"""
            SELECT state, count(*)
            FROM {bigfoot_table}
            GROUP BY state
            ORDER BY count(*) DESC, state
            LIMIT 5
            """
        )
        require(d["top_states"], f"bigfoot import produced no aggregate rows: {d}")

    location_csv = next(
        (Path(p) for p in BIGFOOT_LOCATION_CSV_CANDIDATES if Path(p).exists()), None
    )
    with h.step("imports", "optional_bigfoot_locations_all_columns", optional=True) as d:
        if location_csv is None:
            raise SkipStep(f"bigfoot locations CSV not found; tried {BIGFOOT_LOCATION_CSV_CANDIDATES}")
        full_table = f"public.e2e_bigfoot_full_{suffix}"
        imported["bigfoot_full_table"] = full_table
        h.created_tables.append(full_table)
        with location_csv.open(newline="", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            columns = [str(col).strip().lower() for col in (reader.fieldnames or [])]
            if not columns:
                raise AssertionError("bigfoot locations CSV had no header")
            column_defs = ",\n              ".join(f"{sql_ident(col)} text" for col in columns)
            h.sql(f"CREATE TABLE {full_table} ({column_defs}) USING rvbbit")
            copy_cols = ", ".join(sql_ident(col) for col in columns)
            copied = 0
            with h.conn.cursor().copy(f"COPY {full_table} ({copy_cols}) FROM STDIN") as cp:
                for row in reader:
                    cp.write_row([row.get(col, "") for col in columns])
                    copied += 1
                    if copied >= MODEL_TRAINING_ROWS:
                        break
        h.sql(f"ANALYZE {full_table}")
        d["csv_path"] = str(location_csv)
        d["columns"] = columns
        d["rows"] = h.scalar(f"SELECT count(*) FROM {full_table}")
        require(d["rows"] == copied and copied >= 25, f"bigfoot full-column copy failed: {d}")
        d["exported"] = h.scalar(f"SELECT rvbbit.export_to_parquet('{full_table}'::regclass)")
        d["class_counts"] = h.sql(
            f"""
            SELECT {sql_ident('class')}, count(*)
            FROM {full_table}
            GROUP BY {sql_ident('class')}
            ORDER BY count(*) DESC, {sql_ident('class')}
            LIMIT 5
            """
        )
        require(len(d["class_counts"]) >= 2, f"bigfoot class distribution too narrow: {d}")

    return imported


def register_echo_operator(h: E2EHarness, suffix: str, batch_size: int = 8) -> tuple[str, str]:
    backend = f"e2e_echo_{suffix}"
    op = f"e2e_upper_{suffix}"
    h.created_backends.append(backend)
    h.created_ops.append(op)
    h.sql(
        """
        SELECT rvbbit.register_backend(
          backend_name => %s,
          backend_endpoint => %s,
          backend_batch_size => %s,
          backend_max_concur => 4,
          backend_timeout_ms => 5000
        )
        """,
        (backend, ECHO_PREDICT, batch_size),
    )
    h.sql("SELECT rvbbit.reload_backends()")
    h.sql(
        """
        SELECT rvbbit.create_operator(
          op_name => %s,
          op_shape => 'scalar',
          op_arg_names => ARRAY['text'],
          op_return_type => 'text',
          op_system => 'unused',
          op_user => 'unused',
          op_steps => %s::jsonb
        )
        """,
        (
            op,
            json.dumps(
                [
                    {
                        "name": "s",
                        "kind": "specialist",
                        "specialist": backend,
                        "inputs": {"text": "{{ inputs.text }}", "fn": "upper"},
                    }
                ]
            ),
        ),
    )
    return backend, op


def wait_http(url: str, *, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2).read()
            return
        except Exception as exc:
            last_error = exc
            time.sleep(0.5)
    raise RuntimeError(f"service did not become healthy at {url}: {last_error}")


def phase_model_training(h: E2EHarness, imported: dict[str, str | None]) -> None:
    source_table = imported.get("bigfoot_full_table")
    with h.step("ml", "sql_trained_bigfoot_classifier", optional=True) as d:
        if not source_table:
            raise SkipStep("full-column bigfoot table unavailable")
        trainer = Path(RVBBIT_TRAINER)
        if not trainer.exists():
            raise SkipStep(f"rvbbit-trainer not found at {trainer}")

        suffix = short_id()
        model_name = f"e2e_bigfoot_class_{suffix}"
        backend_name = f"e2e_bigfoot_class_backend_{suffix}"
        operator_name = f"predict_bigfoot_class_{suffix}"
        h.created_ml_models.append(model_name)
        h.created_backends.append(backend_name)
        h.created_ops.append(operator_name)

        feature_schema = [
            {"name": "year_num", "type": "float8"},
            {"name": "latitude", "type": "float8"},
            {"name": "longitude", "type": "float8"},
            {"name": "population", "type": "float8"},
            {"name": "season", "type": "text"},
            {"name": "state", "type": "text"},
            {"name": "county", "type": "text"},
            {"name": "nearesttown", "type": "text"},
            {"name": "nearestroad", "type": "text"},
            {"name": "environment", "type": "text"},
        ]
        source_sql = f"""
            SELECT
              CASE WHEN {sql_ident('year')} ~ '^-?[0-9]+(\\.[0-9]+)?$'
                   THEN {sql_ident('year')}::float8 END AS year_num,
              CASE WHEN {sql_ident('latitude')} ~ '^-?[0-9]+(\\.[0-9]+)?$'
                   THEN {sql_ident('latitude')}::float8 END AS latitude,
              CASE WHEN {sql_ident('longitude')} ~ '^-?[0-9]+(\\.[0-9]+)?$'
                   THEN {sql_ident('longitude')}::float8 END AS longitude,
              CASE WHEN {sql_ident('population')} ~ '^-?[0-9]+(\\.[0-9]+)?$'
                   THEN {sql_ident('population')}::float8 END AS population,
              {sql_ident('season')},
              {sql_ident('state')},
              {sql_ident('county')},
              {sql_ident('nearesttown')},
              {sql_ident('nearestroad')},
              {sql_ident('environment')},
              {sql_ident('class')} AS class_label
            FROM {source_table}
            WHERE {sql_ident('class')} IN ('Class A', 'Class B')
        """
        run_id = h.scalar(
            """
            SELECT rvbbit.train_model(
              model_name => %s,
              source_sql => %s,
              target_column => 'class_label',
              task => 'classification',
              feature_schema => %s::jsonb,
              training_opts => %s::jsonb,
              description => 'E2E classifier over all imported BFRO location columns.'
            )
            """,
            (
                model_name,
                source_sql,
                json.dumps(feature_schema),
                json.dumps(
                    {
                        "estimator": "random_forest",
                        "n_estimators": 32,
                        "random_state": 13,
                        "test_size": 0.25,
                        "min_holdout_rows": 20,
                    }
                ),
            ),
        )
        d["run_id"] = str(run_id)

        endpoint_port = free_port()
        endpoint = f"http://bench:{endpoint_port}/predict"
        output_root = h.artifact_dir / "trained-models"
        cmd = [
            sys.executable,
            str(trainer),
            "train-run",
            str(run_id),
            "--dsn",
            RVBBIT_DSN,
            "--output-root",
            str(output_root),
            "--force",
            "--no-complete",
            "--endpoint-url",
            endpoint,
            "--backend-name",
            backend_name,
            "--operator-name",
            operator_name,
            "--batch-size",
            "32",
            "--max-concurrent",
            "2",
        ]
        env = os.environ.copy()
        env["RVBBIT_DSN"] = RVBBIT_DSN
        trained = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            timeout=180,
            check=False,
            env=env,
        )
        d["trainer_returncode"] = trained.returncode
        d["trainer_stdout"] = trained.stdout[-4000:]
        d["trainer_stderr"] = trained.stderr[-4000:]
        require(trained.returncode == 0, f"rvbbit-trainer failed: {d}")
        payload = json.loads(trained.stdout[trained.stdout.find("{") :])
        out_dir = Path(payload["output_dir"])
        d["output_dir"] = str(out_dir)
        d["metrics"] = payload.get("metrics")
        require((out_dir / "model.joblib").exists(), f"trainer did not write model.joblib: {out_dir}")
        require((out_dir / "config.json").exists(), f"trainer did not write config.json: {out_dir}")

        sidecar_log = h.artifact_dir / f"{operator_name}.log"
        log_fh = sidecar_log.open("ab")
        manifest = json.loads((out_dir / "rvbbit.backend.yaml").read_text())
        runtime = manifest.get("runtime") or {}
        source = manifest.get("source") or {}
        sidecar_env = env.copy()
        sidecar_env.update({str(k): str(v) for k, v in (runtime.get("env") or {}).items()})
        if runtime.get("handler"):
            sidecar_env["RVBBIT_CAPABILITY_HANDLER"] = str(runtime["handler"])
        if runtime.get("device"):
            sidecar_env["RVBBIT_CAPABILITY_DEVICE"] = str(runtime["device"])
        if source.get("model"):
            sidecar_env["RVBBIT_CAPABILITY_MODEL"] = str(source["model"])
        if source.get("revision"):
            sidecar_env["RVBBIT_CAPABILITY_REVISION"] = str(source["revision"])
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "main:app",
                "--host",
                "0.0.0.0",
                "--port",
                str(endpoint_port),
            ],
            cwd=out_dir,
            env=sidecar_env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
        )
        h.child_processes.append(proc)
        d["sidecar_log"] = str(sidecar_log)
        wait_http(f"http://127.0.0.1:{endpoint_port}/health", timeout=90)
        d["sidecar_health"] = http_json(f"http://127.0.0.1:{endpoint_port}/health")

        metrics = json.loads((out_dir / "training_metrics.json").read_text())
        h.sql(
            """
            SELECT rvbbit.complete_model_training(
              run_id => %s::uuid,
              backend_name => %s,
              backend_endpoint => %s,
              backend_transport => 'rvbbit',
              artifact_uri => %s,
              artifact_format => 'joblib',
              metrics => %s::jsonb,
              install_manifest => '{}'::jsonb,
              backend_batch_size => 32,
              backend_max_concur => 2,
              backend_timeout_ms => 120000,
              create_sql_operator => true,
              operator_name => %s,
              operator_arg_name => 'row',
              operator_arg_type => 'jsonb',
              backend_input_key => 'row',
              operator_return_type => 'jsonb',
              operator_parser => 'json'
            )
            """,
            (
                str(run_id),
                backend_name,
                endpoint,
                f"file://{out_dir / 'model.joblib'}",
                json.dumps(metrics),
                operator_name,
            ),
        )
        h.sql("SELECT rvbbit.reload_backends()")
        status = h.sql(
            """
            SELECT name, status, backend_name, operator_name, metrics
            FROM rvbbit.ml_model_status
            WHERE name = %s
            """,
            (model_name,),
        )
        d["model_status"] = status
        require(status and status[0][1] == "active", f"trained model not active: {status}")

        rows = h.sql(
            f"""
            WITH sample AS (
              SELECT
                CASE WHEN {sql_ident('year')} ~ '^-?[0-9]+(\\.[0-9]+)?$'
                     THEN {sql_ident('year')}::float8 END AS year_num,
                CASE WHEN {sql_ident('latitude')} ~ '^-?[0-9]+(\\.[0-9]+)?$'
                     THEN {sql_ident('latitude')}::float8 END AS latitude,
                CASE WHEN {sql_ident('longitude')} ~ '^-?[0-9]+(\\.[0-9]+)?$'
                     THEN {sql_ident('longitude')}::float8 END AS longitude,
                CASE WHEN {sql_ident('population')} ~ '^-?[0-9]+(\\.[0-9]+)?$'
                     THEN {sql_ident('population')}::float8 END AS population,
                {sql_ident('season')},
                {sql_ident('state')},
                {sql_ident('county')},
                {sql_ident('nearesttown')},
                {sql_ident('nearestroad')},
                {sql_ident('environment')},
                {sql_ident('class')} AS class_label
              FROM {source_table}
              WHERE {sql_ident('class')} IN ('Class A', 'Class B')
              ORDER BY {sql_ident('bfroid')}
              LIMIT 8
            )
            SELECT class_label, rvbbit.{operator_name}(to_jsonb(sample)) AS prediction
            FROM sample
            """
        )
        d["predictions"] = rows
        require(len(rows) == 8, f"expected 8 predictions, got {rows}")
        labels = [row[1].get("label") if isinstance(row[1], dict) else None for row in rows]
        require(all(label in {"Class A", "Class B"} for label in labels), f"bad prediction labels: {rows}")
        matches = sum(1 for actual, pred in rows if isinstance(pred, dict) and pred.get("label") == actual)
        d["prediction_matches"] = matches
        require(matches >= 1, f"trained classifier did not match any sample rows: {rows}")


def phase_semantic_echo(h: E2EHarness) -> str | None:
    with h.step("semantic", "echo_sidecar_available", optional=True) as d:
        if not service_alive(f"{ECHO_BASE}/health"):
            raise SkipStep(f"echo sidecar not reachable at {ECHO_BASE}")
        d["health"] = http_json(f"{ECHO_BASE}/health")

    if not service_alive(f"{ECHO_BASE}/health"):
        return None

    suffix = short_id()
    table = f"public.e2e_semantic_{suffix}"
    h.created_tables.append(table)
    _, op = register_echo_operator(h, suffix, batch_size=8)

    with h.step("semantic", "implicit_prewarm_where_order_limit") as d:
        h.sql(f"CREATE TABLE {table} (id int, body text)")
        for i in range(1, 13):
            h.sql(f"INSERT INTO {table} VALUES (%s, %s)", (i, f"item-{i:02}"))
        h.sql(f"ANALYZE {table}")
        h.sql("SELECT rvbbit.flush_cache()")
        h.sql("SELECT rvbbit.reset_query_id()")
        h.sql("DELETE FROM rvbbit.receipts WHERE operator = %s", (op,))
        with contextlib.suppress(Exception):
            http_json(f"{ECHO_BASE}/debug/reset", method="POST")

        rows = h.sql(
            f"""
            SELECT id, rvbbit.{op}(body) AS out
            FROM {table}
            WHERE id >= 4
            ORDER BY id DESC
            LIMIT 3
            """
        )
        d["rows"] = rows
        require(rows == [(12, "ITEM-12"), (11, "ITEM-11"), (10, "ITEM-10")], str(rows))
        receipts = h.scalar("SELECT count(*) FROM rvbbit.receipts WHERE operator = %s", (op,))
        d["receipts"] = receipts
        require(receipts == 3, f"expected 3 receipts from limited implicit prewarm, got {receipts}")
        stats = http_json(f"{ECHO_BASE}/debug/stats")
        d["echo_stats"] = stats
        require(stats["total_inputs"] == 3, f"expected 3 sidecar inputs, got {stats}")

    with h.step("semantic", "explicit_prewarm_batches_and_cache") as d:
        h.sql("SELECT rvbbit.flush_cache()")
        h.sql("DELETE FROM rvbbit.receipts WHERE operator = %s", (op,))
        http_json(f"{ECHO_BASE}/debug/reset", method="POST")
        row = h.sql(
            "SELECT * FROM rvbbit.prewarm_operator(%s, %s)",
            (op, f"SELECT body AS text FROM {table} ORDER BY id"),
        )[0]
        d["prewarm"] = {
            "n_inputs": row[0],
            "n_cache_hits": row[1],
            "n_executed": row[2],
            "n_errors": row[3],
            "wall_ms": row[4],
        }
        require(row[0] == 12 and row[2] == 12 and row[3] == 0, f"unexpected prewarm stats {row}")
        prewarm_stats = http_json(f"{ECHO_BASE}/debug/stats")
        d["prewarm_echo_stats"] = prewarm_stats
        require(prewarm_stats["calls"] == 2, f"expected 2 batches at batch_size=8, got {prewarm_stats}")

        http_json(f"{ECHO_BASE}/debug/reset", method="POST")
        out = h.sql(f"SELECT rvbbit.{op}(body) FROM {table} ORDER BY id")
        d["query_rows"] = len(out)
        query_stats = http_json(f"{ECHO_BASE}/debug/stats")
        d["query_echo_stats"] = query_stats
        require(query_stats["calls"] == 0, f"cache-hit query should not call echo: {query_stats}")

    return op


def phase_semantic_stress(h: E2EHarness) -> str | None:
    with h.step("semantic", "stress_echo_sidecar_available", optional=True):
        if not service_alive(f"{ECHO_BASE}/health"):
            raise SkipStep(f"echo sidecar not reachable at {ECHO_BASE}")

    if not service_alive(f"{ECHO_BASE}/health"):
        return None

    suffix = short_id()
    table = f"public.e2e_semantic_stress_{suffix}"
    h.created_tables.append(table)
    _, op = register_echo_operator(h, f"stress_{suffix}", batch_size=32)
    expected_calls = SEMANTIC_STRESS_ROWS * 2

    with h.step("semantic", "stress_many_scalar_calls_receipts_costs") as d:
        h.sql(f"CREATE TABLE {table} (id int, title text, observed text) USING rvbbit")
        h.sql(
            f"""
            INSERT INTO {table}
            SELECT g,
                   %s::text || ' stress title ' || g::text,
                   %s::text || ' stress observed ' || g::text ||
                     ' with enough words for a semantic call'
            FROM generate_series(1, %s::int) AS g
            """,
            (h.run_id, h.run_id, SEMANTIC_STRESS_ROWS),
        )
        h.sql(f"ANALYZE {table}")
        h.sql("SELECT rvbbit.flush_cache()")
        h.sql(
            """
            DELETE FROM rvbbit.cost_events
            WHERE receipt_id IN (
              SELECT receipt_id FROM rvbbit.receipts WHERE operator = %s
            )
            """,
            (op,),
        )
        h.sql("DELETE FROM rvbbit.receipts WHERE operator = %s", (op,))
        with contextlib.suppress(Exception):
            http_json(f"{ECHO_BASE}/debug/reset", method="POST")

        query_id = h.scalar("SELECT rvbbit.reset_query_id()")
        d["query_id"] = str(query_id)
        d["rows"] = SEMANTIC_STRESS_ROWS
        d["expected_scalar_calls"] = expected_calls

        rows = h.sql(
            f"""
            SELECT id,
                   rvbbit.{op}(title) AS title_out,
                   rvbbit.{op}(observed) AS observed_out
            FROM {table}
            ORDER BY id
            LIMIT %s::int
            """,
            (SEMANTIC_STRESS_ROWS,),
        )
        first_row = rows[0] if rows else None
        last_row = rows[-1] if rows else None
        d["query_result"] = {
            "rows": len(rows),
            "first_row": first_row,
            "last_row": last_row,
        }
        require(len(rows) == SEMANTIC_STRESS_ROWS, f"stress query returned wrong count: {len(rows)}")
        require(
            first_row is not None
            and isinstance(first_row[1], str)
            and "STRESS TITLE" in first_row[1]
            and isinstance(first_row[2], str)
            and "STRESS OBSERVED" in first_row[2],
            f"stress operator output was not uppercased: {first_row}",
        )

        receipt_stats = h.sql(
            """
            SELECT count(*)::int,
                   count(*) FILTER (WHERE query_id = %s::uuid)::int,
                   count(*) FILTER (WHERE error IS NULL)::int,
                   count(DISTINCT inputs_hash)::int
            FROM rvbbit.receipts
            WHERE operator = %s
            """,
            (str(query_id), op),
        )[0]
        d["receipt_stats"] = {
            "total": receipt_stats[0],
            "matching_query_id": receipt_stats[1],
            "without_error": receipt_stats[2],
            "distinct_inputs": receipt_stats[3],
        }
        require(
            receipt_stats == (expected_calls, expected_calls, expected_calls, expected_calls),
            f"semantic stress dropped or duplicated receipts: {receipt_stats}",
        )

        stats = http_json(f"{ECHO_BASE}/debug/stats")
        d["echo_stats"] = stats
        require(
            stats["total_inputs"] == expected_calls,
            f"echo sidecar did not see expected inputs: {stats}",
        )
        require(stats["calls"] > 0, f"echo sidecar saw no batches: {stats}")

        d["cost_events_backfilled"] = h.scalar(
            "SELECT rvbbit.backfill_cost_events_from_receipts(%s)",
            (expected_calls + 50,),
        )
        audit_rows = h.sql(
            """
            SELECT audit_status, count(*)::int
            FROM rvbbit.receipt_cost_audit a
            JOIN rvbbit.receipts r USING (receipt_id)
            WHERE r.operator = %s
              AND r.query_id = %s::uuid
            GROUP BY audit_status
            ORDER BY audit_status
            """,
            (op, str(query_id)),
        )
        d["cost_audit"] = audit_rows
        audited = sum(row[1] for row in audit_rows)
        require(audited == expected_calls, f"cost audit did not cover all stress receipts: {audit_rows}")
        d["cost_events"] = h.scalar(
            """
            SELECT count(*)::int
            FROM rvbbit.cost_events
            WHERE query_id = %s::uuid
              AND receipt_id IN (
                SELECT receipt_id FROM rvbbit.receipts WHERE operator = %s
              )
            """,
            (str(query_id), op),
        )
        require(d["cost_events"] == expected_calls, f"expected one cost event per stress receipt: {d}")

    return op


def phase_backend_failure_audit(h: E2EHarness) -> None:
    suffix = short_id()
    backend = f"e2e_bad_backend_{suffix}"
    op = f"e2e_bad_op_{suffix}"
    h.created_backends.append(backend)
    h.created_ops.append(op)
    with h.step("semantic", "backend_failure_receipt_audit") as d:
        h.sql(
            """
            SELECT rvbbit.register_backend(
              backend_name => %s,
              backend_endpoint => 'http://nope.invalid:9/predict',
              backend_batch_size => 1,
              backend_max_concur => 1,
              backend_timeout_ms => 500
            )
            """,
            (backend,),
        )
        h.sql("SELECT rvbbit.reload_backends()")
        h.sql(
            """
            SELECT rvbbit.create_operator(
              op_name => %s,
              op_shape => 'scalar',
              op_arg_names => ARRAY['text'],
              op_return_type => 'text',
              op_system => 'unused',
              op_user => 'unused',
              op_steps => %s::jsonb
            )
            """,
            (
                op,
                json.dumps(
                    [
                        {
                            "name": "s",
                            "kind": "specialist",
                            "specialist": backend,
                            "inputs": {"text": "{{ inputs.text }}"},
                        }
                    ]
                ),
            ),
        )
        h.sql("SELECT rvbbit.flush_cache()")
        h.sql("DELETE FROM rvbbit.receipts WHERE operator = %s", (op,))
        query_id = h.scalar("SELECT rvbbit.reset_query_id()")
        out = h.scalar(f"SELECT rvbbit.{op}('this should fail cleanly')")
        d["query_id"] = str(query_id)
        d["output"] = out
        require(out == "", f"failed specialist should return empty text, got {out!r}")
        receipt = h.sql(
            """
            SELECT receipt_id, query_id, error, sub_calls->0->>'error'
            FROM rvbbit.receipts
            WHERE operator = %s
            ORDER BY invocation_at DESC
            LIMIT 1
            """,
            (op,),
        )
        d["receipt"] = [
            {
                "receipt_id": str(row[0]),
                "query_id": str(row[1]),
                "error": row[2],
                "sub_call_error": row[3],
            }
            for row in receipt
        ]
        require(receipt, "backend failure did not write a receipt")
        require(str(receipt[0][1]) == str(query_id), f"failure receipt query_id mismatch: {receipt}")
        require(receipt[0][2] or receipt[0][3], f"failure receipt did not capture error: {receipt}")

        d["cost_events_backfilled"] = h.scalar("SELECT rvbbit.backfill_cost_events_from_receipts(50)")
        d["cost_audit"] = h.sql(
            """
            SELECT audit_status, missing_cost_events, cost_event_sub_calls, error_calls
            FROM rvbbit.receipt_cost_audit
            WHERE receipt_id = %s::uuid
            """,
            (str(receipt[0][0]),),
        )
        require(d["cost_audit"], f"failure receipt missing cost audit row: {receipt}")

        if service_alive(f"{ECHO_BASE}/health"):
            _, good_op = register_echo_operator(h, f"after_bad_{suffix}", batch_size=4)
            good = h.scalar(f"SELECT rvbbit.{good_op}('ok')")
            d["post_failure_good_operator"] = good
            require(good == "OK", f"healthy backend failed after bad backend: {good}")


def phase_embeddings(h: E2EHarness, imported: dict[str, str | None]) -> None:
    suffix = short_id()
    backend = f"e2e_stub_embed_{suffix}"
    table = f"public.e2e_embed_{suffix}"
    h.created_backends.append(backend)
    h.created_stub_embeds.append(backend)
    h.created_tables.append(table)
    with h.step("embeddings", "stub_embed_materialize_knn") as d:
        h.sql(
            """
            SELECT rvbbit.register_backend(
              backend_name => %s,
              backend_endpoint => 'stub://128',
              backend_transport => 'stub'
            )
            """,
            (backend,),
        )
        h.sql("SELECT rvbbit.reload_backends()")
        d["embed_dim"] = h.scalar("SELECT cardinality(rvbbit.embed('hello world', %s))", (backend,))
        require(d["embed_dim"] == 128, f"expected 128-dim stub vector, got {d['embed_dim']}")

        h.sql(f"CREATE TABLE {table} (id int, body text) USING rvbbit")
        for i, body in enumerate(
            [
                "refund request after damaged shipment",
                "contract renewal risk and late invoice",
                "happy customer asking about upgrades",
                "refund request after damaged shipment",
            ],
            start=1,
        ):
            h.sql(f"INSERT INTO {table} VALUES (%s, %s)", (i, body))
        h.sql(f"SELECT rvbbit.export_to_parquet('{table}'::regclass)")
        d["materialized"] = h.scalar(
            "SELECT rvbbit.materialize_embeddings(%s::regclass::oid, 'body', %s)",
            (table, backend),
        )
        require(d["materialized"] == 3, f"expected 3 distinct embeddings, got {d['materialized']}")
        rows = h.sql(
            "SELECT value, score FROM rvbbit.knn_text(%s::regclass::oid, 'body', %s, 2, %s)",
            (table, "damaged refund", backend),
        )
        d["knn"] = rows
        require(len(rows) == 2, f"expected two KNN rows, got {rows}")

    source_table = imported.get("bigfoot_table") or imported.get("copy_table")
    local_backend = f"e2e_local_embed_{suffix}"
    h.created_backends.append(local_backend)
    with h.step("embeddings", "local_embed_transport_optional", optional=True) as d:
        if not source_table:
            raise SkipStep("no imported text table available for local embedding check")
        default_row = h.sql(
            """
            SELECT transport, endpoint_url, transport_opts->>'model'
            FROM rvbbit.backends
            WHERE name = 'embed'
            """
        )
        if default_row:
            d["default_embed_backend"] = {
                "transport": default_row[0][0],
                "endpoint_url": default_row[0][1],
                "model": default_row[0][2],
            }
        h.sql(
            """
            SELECT rvbbit.register_backend(
              backend_name => %s,
              backend_endpoint => 'local://e2e',
              backend_transport => 'local_embed',
              backend_batch_size => 32,
              backend_max_concur => 1,
              backend_timeout_ms => 120000,
              backend_opts => '{"model":"bge-small-en-v1.5"}'::jsonb,
              backend_description => 'E2E temporary local CPU embedding backend.'
            )
            """,
            (local_backend,),
        )
        h.sql("SELECT rvbbit.reload_backends()")
        d["backend"] = {
            "name": local_backend,
            "transport": "local_embed",
            "endpoint_url": "local://e2e",
            "model": "bge-small-en-v1.5",
        }

        d["embed_dim"] = h.scalar(
            "SELECT cardinality(rvbbit.embed(%s, %s))",
            (f"{h.run_id}: local embedding smoke text", local_backend),
        )
        require(d["embed_dim"] and d["embed_dim"] > 0, f"bad local embed dim: {d}")

        d["source_table"] = source_table
        d["materialized"] = h.scalar(
            "SELECT rvbbit.materialize_embeddings(%s::regclass::oid, 'observed', %s)",
            (source_table, local_backend),
        )
        require(d["materialized"] >= 1, f"local materialize produced no rows: {d}")
        rows = h.sql(
            """
            SELECT value, score
            FROM rvbbit.knn_text(%s::regclass::oid, 'observed', %s, 3, %s)
            """,
            (source_table, "road crossing witness report", local_backend),
        )
        d["knn"] = rows
        require(rows, "local KNN returned no rows")
        d["cache_stats"] = h.sql(
            """
            SELECT specialist, n_entries, dim
            FROM rvbbit.embedding_cache_stats()
            WHERE specialist = %s
            """,
            (local_backend,),
        )


def phase_python(h: E2EHarness) -> None:
    """Managed CPython runtime as a first-class operator workflow node."""

    with h.step("python", "runtime_registered_ready") as d:
        rows = h.sql(
            """
            SELECT name, endpoint_url, status, runtime_source
            FROM rvbbit.python_runtimes
            WHERE name = 'python_default'
            """
        )
        d["python_default"] = rows[0] if rows else None
        require(rows, "python_default runtime is not registered")
        name, endpoint_url, status, source = rows[0]
        require(name == "python_default", f"unexpected runtime row: {rows[0]}")
        require(status == "ready", f"python_default is not ready: {rows[0]}")
        require(source == "warren", f"python_default was not installed by Warren: {rows[0]}")
        configured = h.scalar("SELECT rvbbit.python_runtime_endpoint()")
        d["configured_endpoint"] = configured
        require(
            str(configured).rstrip("/") == str(endpoint_url).rstrip("/"),
            f"python runtime endpoint setting {configured!r} does not match registered {endpoint_url!r}",
        )

    suffix = short_id()
    table = f"public.e2e_customer_dim_{suffix}"
    env = f"e2e_pyenv_{suffix}"
    handler = f"e2e_sla_score_{suffix}"
    op = f"e2e_ticket_sla_{suffix}"
    h.created_tables.append(table)
    h.created_python_envs.append(env)
    h.created_python_handlers.append(handler)
    h.created_ops.append(op)

    code = r'''
import re

def run(inputs):
    message = str(inputs.get("message") or "")
    tier = str(inputs.get("tier") or "standard").lower()
    revenue = float(inputs.get("annual_revenue") or 0)
    open_tickets = int(inputs.get("open_tickets") or 0)
    outage = re.search(r"\b(outage|down|cannot access|can't access|checkout)\b", message, re.I) is not None
    score = 0.0
    flags = []
    if tier in {"enterprise", "strategic"}:
        score += 0.35
        flags.append("high_value_account")
    if revenue >= 1000000:
        score += 0.25
        flags.append("revenue_risk")
    if open_tickets >= 3:
        score += 0.20
        flags.append("repeat_contact")
    if outage:
        score += 0.35
        flags.append("possible_outage")
    priority = "urgent" if score >= 0.70 else "elevated" if score >= 0.35 else "standard"
    return {
        "priority": priority,
        "score": round(min(score, 1.0), 3),
        "flags": flags,
        "normalized_message": " ".join(message.lower().split()),
    }
'''

    with h.step("python", "sql_lookup_python_operator_with_ward") as d:
        h.sql(f"CREATE TABLE {table} (id int PRIMARY KEY, tier text, annual_revenue float8)")
        h.sql(
            f"""
            INSERT INTO {table}
            VALUES (101, 'enterprise', 2400000), (202, 'standard', 12000)
            """
        )
        h.sql(
            """
            SELECT rvbbit.create_python_env(
              env_name => %s,
              python_version => '3.12',
              requirements => ARRAY[]::text[],
              runtime_name => 'python_default',
              timeout_ms => 3000
            )
            """,
            (env,),
        )
        h.sql(
            """
            SELECT rvbbit.create_python_handler(
              handler_name => %s,
              env_name => %s,
              code => %s,
              entrypoint => 'run',
              description => 'E2E deterministic SLA scorer'
            )
            """,
            (handler, env, code),
        )
        steps = [
            {
                "name": "cust",
                "kind": "sql",
                "sql": f"SELECT tier, annual_revenue FROM {table} WHERE id = $1::int",
                "params": ["{{ inputs.customer_id }}"],
            },
            {
                "name": "score",
                "kind": "python",
                "env": env,
                "handler": handler,
                "inputs": {
                    "message": "{{ inputs.message }}",
                    "open_tickets": "{{ inputs.open_tickets }}",
                    "tier": "{{ steps.cust.output.tier }}",
                    "annual_revenue": "{{ steps.cust.output.annual_revenue }}",
                },
            },
        ]
        h.sql(
            """
            SELECT rvbbit.create_operator(
              op_name => %s,
              op_arg_names => ARRAY['customer_id','message','open_tickets'],
              op_arg_types => ARRAY['text','text','text'],
              op_return_type => 'jsonb',
              op_parser => 'json',
              op_steps => %s::jsonb
            )
            """,
            (op, json.dumps(steps)),
        )
        h.sql(
            "SELECT rvbbit.set_operator_wards(%s, %s::jsonb)",
            (
                op,
                json.dumps(
                    {
                        "post": [
                            {
                                "validator": {
                                    "sql": "($output::jsonb ? 'priority') AND (($output::jsonb->>'priority') IN ('standard','elevated','urgent'))"
                                },
                                "mode": "blocking",
                            }
                        ]
                    }
                ),
            ),
        )
        h.sql("SELECT rvbbit.flush_cache()")
        h.sql("DELETE FROM rvbbit.receipts WHERE operator = %s", (op,))
        out = h.scalar(
            f"SELECT rvbbit.{sql_ident(op)}(%s, %s, %s)",
            ("101", "Checkout is down and our team cannot access invoices", "4"),
        )
        d["output"] = out
        require(isinstance(out, dict), f"python operator returned non-json object: {out}")
        require(out.get("priority") == "urgent", f"unexpected Python priority: {out}")
        require("high_value_account" in (out.get("flags") or []), f"missing account flag: {out}")
        require("possible_outage" in (out.get("flags") or []), f"missing outage flag: {out}")
        kinds = h.scalar(
            """
            SELECT jsonb_path_query_array(sub_calls, '$[*].kind')
            FROM rvbbit.receipts
            WHERE operator = %s
            ORDER BY invocation_at DESC
            LIMIT 1
            """,
            (op,),
        )
        d["sub_call_kinds"] = kinds
        require(kinds == ["sql", "python"], f"bad Python operator sub-call audit: {kinds}")


def phase_mcp(h: E2EHarness) -> None:
    server = f"e2e_mcp_{short_id()}"
    op = f"e2e_mcp_search_{short_id()}"
    chained_op = f"e2e_mcp_chain_{short_id()}"
    chain_backend = f"e2e_mcp_chain_echo_{short_id()}"
    h.created_mcp_servers.append(server)
    h.created_ops.append(op)
    h.created_ops.append(chained_op)
    h.created_backends.append(chain_backend)
    with h.step("mcp", "gateway_runtime_ready") as d:
        rows = h.sql(
            """
            SELECT name, endpoint_url, status, gateway_source, health
            FROM rvbbit.mcp_gateways
            WHERE status = 'ready'
            ORDER BY (name = 'mcp_default') DESC, updated_at DESC
            LIMIT 1
            """
        )
        require(rows, "no ready MCP Gateway runtime registered")
        name, endpoint_url, status, source, health = rows[0]
        d["gateway"] = {
            "name": name,
            "endpoint_url": endpoint_url,
            "status": status,
            "gateway_source": source,
            "health": health,
        }
        require(name == "mcp_default", f"unexpected MCP gateway runtime: {rows[0]}")
        require(source == "warren", f"MCP gateway was not installed by Warren: {rows[0]}")
        configured = h.scalar("SELECT rvbbit.mcp_gateway_endpoint()")
        d["configured_endpoint"] = configured
        require(
            str(configured).rstrip("/") == str(endpoint_url).rstrip("/"),
            f"MCP gateway endpoint setting {configured!r} does not match registered {endpoint_url!r}",
        )

    with h.step("mcp", "register_refresh_call_and_audit") as d:
        h.sql(
            """
            SELECT rvbbit.register_mcp_server(
              server_name => %s,
              server_transport => 'stdio',
              server_command => 'python',
              server_args => ARRAY['/opt/mcp-test-server/main.py']
            )
            """,
            (server,),
        )
        d["tools_refreshed"] = h.scalar("SELECT rvbbit.refresh_mcp_server(%s)", (server,))
        require(d["tools_refreshed"] >= 3, f"expected >=3 tools, got {d['tools_refreshed']}")
        query_id = h.scalar("SELECT rvbbit.reset_query_id()")
        d["query_id"] = str(query_id)
        out = h.scalar(
            "SELECT rvbbit.mcp_call(%s, 'add', '{\"a\":2,\"b\":5}'::jsonb)",
            (server,),
        )
        d["call"] = out
        text = " ".join(
            item.get("text", "") for item in out.get("content", []) if isinstance(item, dict)
        )
        require(out.get("isError") is False and "7" in text, f"unexpected MCP result {out}")
        row = h.sql(
            """
            SELECT tool, query_id
            FROM rvbbit.mcp_invocations
            WHERE server = %s
            ORDER BY invocation_at DESC
            LIMIT 1
            """,
            (server,),
        )[0]
        d["last_invocation"] = {"tool": row[0], "query_id": str(row[1])}
        require(str(row[1]) == str(query_id), f"MCP query_id mismatch: {row}")

    with h.step("mcp", "rows_surface_and_error_audit") as d:
        rows = h.sql(
            f"""
            SELECT row->>'name'
            FROM rvbbit.mcp_rows(
              %s,
              'list_items',
              '{{"n":4}}'::jsonb
            ) AS row
            ORDER BY row->>'name'
            """,
            (server,),
        )
        d["list_items"] = [row[0] for row in rows]
        require(d["list_items"] == ["item0", "item1", "item2", "item3"], f"bad mcp_rows list: {rows}")

        search_rows = h.sql(
            """
            SELECT row->>'name'
            FROM rvbbit.mcp_rows(%s, 'search', '{"q":"delta"}'::jsonb) AS row
            ORDER BY row->>'name'
            """,
            (server,),
        )
        d["search_items"] = [row[0] for row in search_rows]
        require(
            d["search_items"] == ["delta-a", "delta-b", "delta-c"],
            f"bad mcp_rows nested items: {search_rows}",
        )

        echo_rows = h.sql(
            "SELECT row FROM rvbbit.mcp_rows(%s, 'echo', '{\"text\":\"plain\"}'::jsonb) AS row",
            (server,),
        )
        d["echo_rows"] = echo_rows
        require(echo_rows and echo_rows[0][0] in ("plain", '"plain"'), f"bad mcp_rows echo: {echo_rows}")

        query_id = h.scalar("SELECT rvbbit.reset_query_id()")
        out = h.scalar("SELECT rvbbit.mcp_call(%s, 'failing', '{}'::jsonb)", (server,))
        d["failure_call"] = out
        require(out.get("isError") is True, f"failing MCP tool did not return error: {out}")
        audit = h.sql(
            """
            SELECT tool, error, query_id
            FROM rvbbit.mcp_invocations
            WHERE server = %s AND tool = 'failing'
            ORDER BY invocation_at DESC
            LIMIT 1
            """,
            (server,),
        )
        d["failure_audit"] = [
            {"tool": row[0], "error": row[1], "query_id": str(row[2])}
            for row in audit
        ]
        require(audit and audit[0][1], f"MCP failure was not audited: {audit}")
        require(str(audit[0][2]) == str(query_id), f"MCP failure query_id mismatch: {audit}")

    with h.step("mcp", "operator_node_flow") as d:
        steps = [
            {
                "name": "fetch",
                "kind": "mcp",
                "server": server,
                "tool": "search",
                "inputs": {"q": "{{ inputs.topic }}"},
            }
        ]
        h.sql(
            """
            SELECT rvbbit.create_operator(
              op_name => %s,
              op_arg_names => ARRAY['topic'],
              op_return_type => 'jsonb',
              op_steps => %s::jsonb
            )
            """,
            (op, json.dumps(steps)),
        )
        query_id = h.scalar("SELECT rvbbit.reset_query_id()")
        out = h.scalar(f"SELECT rvbbit.{op}('alpha')")
        d["query_id"] = str(query_id)
        d["operator"] = op
        d["output"] = out
        require(out["query"] == "alpha", f"unexpected MCP operator query: {out}")
        require(out["total"] == 3, f"unexpected MCP operator total: {out}")
        require(
            {item["name"] for item in out["items"]} == {"alpha-a", "alpha-b", "alpha-c"},
            f"unexpected MCP operator items: {out}",
        )
        receipt = h.sql(
            """
            SELECT query_id, sub_calls->0->>'kind', sub_calls->0->>'model', error
            FROM rvbbit.receipts
            WHERE operator = %s
            ORDER BY invocation_at DESC
            LIMIT 1
            """,
            (op,),
        )
        d["receipt"] = [
            {"query_id": str(row[0]), "kind": row[1], "model": row[2], "error": row[3]}
            for row in receipt
        ]
        require(receipt, "missing MCP operator receipt")
        require(str(receipt[0][0]) == str(query_id), f"MCP operator query_id mismatch: {receipt}")
        require(receipt[0][1] == "mcp", f"MCP operator receipt missing mcp subcall: {receipt}")
        require(receipt[0][2] == f"{server}.search", f"MCP operator receipt model mismatch: {receipt}")
        require(receipt[0][3] is None, f"MCP operator receipt has error: {receipt}")

    with h.step("mcp", "operator_chain_mcp_code_specialist") as d:
        if not service_alive(f"{ECHO_BASE}/health"):
            raise SkipStep(f"echo sidecar not reachable at {ECHO_BASE}")
        h.sql(
            """
            SELECT rvbbit.register_backend(
              backend_name => %s,
              backend_endpoint => %s,
              backend_batch_size => 4,
              backend_max_concur => 2,
              backend_timeout_ms => 5000
            )
            """,
            (chain_backend, ECHO_PREDICT),
        )
        h.sql("SELECT rvbbit.reload_backends()")
        steps = [
            {
                "name": "fetch",
                "kind": "mcp",
                "server": server,
                "tool": "echo",
                "inputs": {"text": "{{ inputs.text }}"},
            },
            {
                "name": "up",
                "kind": "code",
                "fn": "uppercase",
                "inputs": {"text": "{{ steps.fetch.output }}"},
            },
            {
                "name": "rev",
                "kind": "specialist",
                "specialist": chain_backend,
                "inputs": {"text": "{{ steps.up.output }}", "fn": "reverse"},
            },
        ]
        h.sql(
            """
            SELECT rvbbit.create_operator(
              op_name => %s,
              op_arg_names => ARRAY['text'],
              op_return_type => 'text',
              op_steps => %s::jsonb
            )
            """,
            (chained_op, json.dumps(steps)),
        )
        query_id = h.scalar("SELECT rvbbit.reset_query_id()")
        out = h.scalar(f"SELECT rvbbit.{chained_op}('hello')")
        d["query_id"] = str(query_id)
        d["operator"] = chained_op
        d["output"] = out
        require(out == "OLLEH", f"unexpected mcp->code->specialist output: {out}")
        receipt = h.sql(
            """
            SELECT query_id,
                   sub_calls->0->>'kind',
                   sub_calls->1->>'kind',
                   sub_calls->2->>'kind',
                   error
            FROM rvbbit.receipts
            WHERE operator = %s
            ORDER BY invocation_at DESC
            LIMIT 1
            """,
            (chained_op,),
        )
        d["receipt"] = [
            {
                "query_id": str(row[0]),
                "kinds": [row[1], row[2], row[3]],
                "error": row[4],
            }
            for row in receipt
        ]
        require(receipt, "missing chained MCP operator receipt")
        require(str(receipt[0][0]) == str(query_id), f"chained operator query_id mismatch: {receipt}")
        require(receipt[0][1:4] == ("mcp", "code", "specialist"), f"bad chained receipt: {receipt}")
        require(receipt[0][4] is None, f"chained operator receipt error: {receipt}")


def phase_kg(h: E2EHarness) -> None:
    suffix = short_id()
    graph = f"e2e_graph_{suffix}"
    customer_kind = f"e2e_customer_{suffix}"
    issue_kind = f"e2e_issue_{suffix}"
    with h.step("kg", "ingest_triples_and_traverse") as d:
        query_id = uuid.uuid4()
        raw = json.dumps(
            [
                {
                    "subject_kind": customer_kind,
                    "subject": "Acme Corp",
                    "predicate": "reported",
                    "object_kind": issue_kind,
                    "object": "late shipment",
                    "confidence": 0.91,
                    "evidence": "Acme reported late shipments in the field notes.",
                    "properties": {"run_id": h.run_id},
                },
                {
                    "subject_kind": customer_kind,
                    "subject": "Acme Corp",
                    "predicate": "observed",
                    "object_kind": issue_kind,
                    "object": "night road crossing",
                    "confidence": 0.82,
                    "evidence": "A night road crossing was observed.",
                    "properties": {"run_id": h.run_id},
                },
            ]
        )
        ingest_sql = (
            f"SELECT *, '42'::text AS source_pk, 'observed'::text AS source_column, "
            f"'{query_id}'::uuid AS query_id, '{graph}'::text AS graph_id "
            f"FROM rvbbit.triples_json_rows('{raw.replace(chr(39), chr(39) * 2)}'::jsonb)"
        )
        d["ingested"] = h.scalar(
            """
            SELECT rvbbit.kg_ingest_triples(
              %s,
              source_table => NULL,
              match_threshold => 0.0,
              graph => %s
            )
            """,
            (ingest_sql, graph),
        )
        require(d["ingested"] == 2, f"expected 2 triples ingested, got {d['ingested']}")
        neighbors = h.sql(
            """
            SELECT predicate, to_label
            FROM rvbbit.kg_neighbors(%s, 'Acme Corp', 1, 'out', '', 0.0, %s)
            ORDER BY predicate, to_label
            """,
            (customer_kind, graph),
        )
        d["neighbors"] = neighbors
        require(len(neighbors) == 2, f"expected two KG neighbors, got {neighbors}")
        evidence_query_ids = h.sql(
            """
            SELECT DISTINCT ev.query_id
            FROM rvbbit.kg_evidence ev
            WHERE ev.graph_id = %s
            """,
            (graph,),
        )
        d["evidence_query_ids"] = [str(row[0]) for row in evidence_query_ids]
        require(str(query_id) in d["evidence_query_ids"], "KG evidence query_id missing")


def phase_kg_imported_text(h: E2EHarness, imported: dict[str, str | None]) -> None:
    source_table = imported.get("bigfoot_table") or imported.get("copy_table")
    if not source_table:
        with h.step("kg", "imported_text_graph", optional=True):
            raise SkipStep("no imported text table available for KG graph check")
        return

    suffix = short_id()
    graph = f"e2e_import_graph_{suffix}"
    report_kind = f"e2e_report_{suffix}"
    state_kind = f"e2e_state_{suffix}"
    clue_kind = f"e2e_clue_{suffix}"
    pk_expr = "bfroid" if imported.get("bigfoot_table") else "id::text"

    with h.step("kg", "imported_text_graph") as d:
        query_id = h.scalar("SELECT rvbbit.reset_query_id()")
        first_label = h.scalar(
            f"""
            SELECT {pk_expr}
            FROM {source_table}
            WHERE coalesce(observed, '') <> ''
            ORDER BY 1
            LIMIT 1
            """
        )
        require(first_label is not None, f"{source_table} has no observed text rows")

        reported = h.scalar(
            f"""
            WITH src AS (
              SELECT {pk_expr} AS source_id, title, state, observed
              FROM {source_table}
              WHERE coalesce(observed, '') <> ''
              ORDER BY 1
              LIMIT %s::int
            ),
            edges AS (
              SELECT rvbbit.kg_assert_edge(
                %s::text,
                source_id,
                'reported_in',
                %s::text,
                lower(coalesce(nullif(state, ''), 'unknown')),
                0.84,
                jsonb_build_object(
                  'text', observed,
                  'source', 'e2e_imported_text',
                  'source_table', %s::text,
                  'source_pk', source_id,
                  'title', title
                ),
                jsonb_build_object(
                  'run_id', %s::text,
                  'source_table', %s::text,
                  'source_pk', source_id
                ),
                '',
                0.0,
                %s::text
              ) AS edge_id
              FROM src
            )
            SELECT count(*) FROM edges
            """,
            (BIGFOOT_ROWS, report_kind, state_kind, source_table, h.run_id, source_table, graph),
        )
        clues = h.scalar(
            f"""
            WITH src AS (
              SELECT {pk_expr} AS source_id, title, observed
              FROM {source_table}
              WHERE coalesce(observed, '') <> ''
              ORDER BY 1
              LIMIT %s::int
            ),
            labeled AS (
              SELECT source_id, title, observed,
                     CASE
                       WHEN lower(coalesce(observed, '') || ' ' || coalesce(title, '')) LIKE '%%road%%'
                         THEN 'road crossing'
                       WHEN lower(coalesce(observed, '') || ' ' || coalesce(title, '')) LIKE '%%footprint%%'
                         OR lower(coalesce(observed, '') || ' ' || coalesce(title, '')) LIKE '%%track%%'
                         THEN 'tracks or footprints'
                       WHEN lower(coalesce(observed, '') || ' ' || coalesce(title, '')) LIKE '%%saw%%'
                         OR lower(coalesce(observed, '') || ' ' || coalesce(title, '')) LIKE '%%observed%%'
                         THEN 'visual encounter'
                       ELSE 'field report'
                     END AS clue
              FROM src
            ),
            edges AS (
              SELECT rvbbit.kg_assert_edge(
                %s::text,
                source_id,
                'mentions',
                %s::text,
                clue,
                0.78,
                jsonb_build_object(
                  'text', observed,
                  'source', 'e2e_imported_text',
                  'source_table', %s::text,
                  'source_pk', source_id,
                  'title', title
                ),
                jsonb_build_object(
                  'run_id', %s::text,
                  'source_table', %s::text,
                  'source_pk', source_id,
                  'derived_clue', clue
                ),
                '',
                0.0,
                %s::text
              ) AS edge_id
              FROM labeled
            )
            SELECT count(*) FROM edges
            """,
            (BIGFOOT_ROWS, report_kind, clue_kind, source_table, h.run_id, source_table, graph),
        )
        d["source_table"] = source_table
        d["graph"] = graph
        d["query_id"] = str(query_id)
        d["reported_edges"] = reported
        d["clue_edges"] = clues
        require(reported and clues, f"imported KG edges were not created: {d}")

        d["node_count"] = h.scalar("SELECT count(*) FROM rvbbit.kg_nodes WHERE graph_id = %s", (graph,))
        d["edge_count"] = h.scalar("SELECT count(*) FROM rvbbit.kg_edges WHERE graph_id = %s", (graph,))
        d["evidence_count"] = h.scalar(
            "SELECT count(*) FROM rvbbit.kg_evidence WHERE graph_id = %s",
            (graph,),
        )
        require(d["node_count"] >= 3 and d["edge_count"] >= 2, f"sparse imported KG graph: {d}")
        require(d["evidence_count"] >= 2, f"imported KG evidence missing: {d}")

        neighbors = h.sql(
            """
            SELECT predicate, to_kind, to_label
            FROM rvbbit.kg_neighbors(%s, %s, 1, 'out', '', 0.0, %s)
            ORDER BY predicate, to_label
            """,
            (report_kind, first_label, graph),
        )
        d["first_report"] = first_label
        d["neighbors"] = neighbors
        require(len(neighbors) >= 2, f"imported KG report should have >=2 neighbors: {neighbors}")

        context_rows = h.sql(
            """
            SELECT predicate, to_label, evidence_count, evidence
            FROM rvbbit.kg_context(%s, %s, 1, 10, 'out', true, '', 0.0, %s)
            ORDER BY context_rank
            """,
            (report_kind, first_label, graph),
        )
        d["context"] = [
            {
                "predicate": row[0],
                "to_label": row[1],
                "evidence_count": row[2],
                "evidence": row[3],
            }
            for row in context_rows
        ]
        require(context_rows, "imported KG context returned no rows")
        require(any(row[2] >= 1 for row in context_rows), f"imported KG context lacks evidence: {context_rows}")
        evidence_query_ids = h.sql(
            """
            SELECT DISTINCT query_id
            FROM rvbbit.kg_evidence
            WHERE graph_id = %s
            """,
            (graph,),
        )
        d["evidence_query_ids"] = [str(row[0]) for row in evidence_query_ids]
        require(str(query_id) in d["evidence_query_ids"], "imported KG evidence query_id missing")


def phase_warren(h: E2EHarness) -> None:
    node = f"e2e_warren_{short_id()}"
    h.created_warren_nodes.append(node)
    with h.step("warren", "catalog_node_metrics_job") as d:
        node_id = h.scalar(
            """
            SELECT rvbbit.register_warren_node(
              %s,
              'http://127.0.0.1:0',
              '{"capability":true,"docker":true,"gpu":false}'::jsonb,
              '{"cpu":4,"memory_gb":16}'::jsonb,
              'e2e'
            )
            """,
            (node,),
        )
        d["node_id"] = str(node_id)
        h.sql(
            """
            SELECT rvbbit.record_warren_metrics(
              %s,
              '{"system":{"cpu":{"usage_pct":12.5},"load1":0.2,
                          "memory":{"used_bytes":1024,"total_bytes":4096}},
                "summary":{"gpu_count":0}}'::jsonb
            )
            """,
            (node,),
        )
        manifest = {
            "name": f"e2e-warren-echo-{short_id()}",
            "version": "0.0.0",
            "runtime": {"handler": "echo"},
            "backend": {"name": f"e2e_warren_backend_{short_id()}"},
        }
        job_id = h.scalar(
            "SELECT rvbbit.deploy_capability(%s::jsonb, %s::jsonb, %s)",
            (json.dumps(manifest), '{"capability":true,"docker":true,"gpu":false}', None),
        )
        d["job_id"] = str(job_id)
        h.created_warren_jobs.append(str(job_id))
        claimed = h.sql("SELECT job_id, name FROM rvbbit.claim_warren_job(%s)", (node,))
        d["claimed"] = [(str(row[0]), row[1]) for row in claimed]
        require(claimed and str(claimed[0][0]) == str(job_id), f"job was not claimable: {claimed}")
        inventory_rows = h.sql(
            "SELECT node_name, node_status FROM rvbbit.warren_inventory WHERE node_name = %s LIMIT 1",
            (node,),
        )
        d["inventory"] = inventory_rows
        require(inventory_rows, "warren_inventory did not include e2e node")


def phase_costs(h: E2EHarness, semantic_op: str | None) -> None:
    with h.step("costs", "receipt_cost_audit") as d:
        d["backfilled"] = h.scalar("SELECT rvbbit.backfill_cost_events_from_receipts(1000)")
        if semantic_op:
            d["semantic_op"] = semantic_op
            d["receipts"] = h.scalar(
                "SELECT count(*) FROM rvbbit.receipts WHERE operator = %s",
                (semantic_op,),
            )
            d["audit"] = h.sql(
                """
                SELECT audit_status, count(*)
                FROM rvbbit.receipt_cost_audit a
                JOIN rvbbit.receipts r USING (receipt_id)
                WHERE r.operator = %s
                GROUP BY audit_status
                ORDER BY audit_status
                """,
                (semantic_op,),
            )
            require(d["receipts"] >= 1, "expected semantic receipts for cost audit")
        d["query_cost_rows"] = h.scalar("SELECT count(*) FROM rvbbit.query_costs")


def phase_diagnostics(h: E2EHarness) -> None:
    with h.step("diagnostics", "doctor_passive") as d:
        rows = h.sql("SELECT area, name, status, detail FROM rvbbit.doctor(false)")
        d["rows"] = [
            {"area": row[0], "name": row[1], "status": row[2], "detail": row[3]}
            for row in rows
        ]
        keys = {(row[0], row[1]) for row in rows}
        require(("core", "extension") in keys, f"doctor missing extension row: {rows}")
        require(("routing", "route_status") in keys, f"doctor missing routing row: {rows}")
        require(("provider", "default") in keys, f"doctor missing provider default row: {rows}")
        require(all(row[2] in {"ok", "warn", "error"} for row in rows), f"bad doctor status: {rows}")

    with h.step("diagnostics", "self_hosted_openai_compatible_provider") as d:
        suffix = short_id()
        backend = f"e2e_local_vllm_{suffix}"
        op = f"e2e_local_vllm_{suffix}"
        model = "local/e2e-compatible-chat"
        h.created_backends.append(backend)
        h.created_ops.append(op)
        with local_openai_chat_server() as server:
            d["endpoint"] = server["endpoint"]
            h.sql(
                """
                SELECT rvbbit.register_backend(
                  backend_name => %s,
                  backend_endpoint => %s,
                  backend_transport => 'openai_chat',
                  backend_max_concur => 2,
                  backend_timeout_ms => 5000,
                  backend_opts => %s::jsonb,
                  backend_description => 'E2E local OpenAI-compatible provider'
                )
                """,
                (backend, server["endpoint"], json.dumps({"model": model})),
            )
            catalog = h.scalar(
                """
                SELECT rvbbit.register_self_hosted_model(
                  provider => 'e2e-local',
                  model => %s,
                  backend_name => %s,
                  display_name => 'E2E local compatible chat',
                  family => 'e2e',
                  capabilities => '["chat"]'::jsonb,
                  cost_policy => 'free')
                """,
                (model, backend),
            )
            d["catalog"] = catalog
            h.sql("SELECT rvbbit.reload_backends()")
            doctor = h.sql(
                """
                SELECT status, detail
                FROM rvbbit.provider_doctor(true)
                WHERE name = %s
                """,
                (backend,),
            )
            d["provider_doctor"] = doctor
            require(doctor and doctor[0][0] == "ok", f"local provider doctor failed: {doctor}")

            h.sql("SELECT rvbbit.set_default_provider(%s)", (backend,))
            try:
                h.sql(
                    """
                    SELECT rvbbit.create_operator(
                      op_name => %s,
                      op_arg_names => ARRAY['text'],
                      op_return_type => 'text',
                      op_system => 'Reply tersely.',
                      op_user => 'Local default {{ inputs.text }}',
                      op_model => %s,
                      op_max_tokens => 12,
                      op_temperature => 0)
                    """,
                    (op, model),
                )
                h.sql("SELECT rvbbit.flush_cache()")
                query_id = h.scalar("SELECT rvbbit.reset_query_id()")
                d["query_id"] = str(query_id)
                out = h.scalar(f"SELECT rvbbit.{op}('provider works')")
                d["output"] = out
                require(
                    isinstance(out, str)
                    and "local-compatible-ok" in out
                    and "provider works" in out,
                    f"unexpected local provider output: {out}",
                )
                h.sql("SELECT rvbbit.backfill_cost_events_from_receipts(1000)")
                cost = h.sql(
                    """
                    SELECT status, cost_source, cost_usd::float8
                    FROM rvbbit.cost_latest
                    WHERE query_id = %s::uuid AND backend = %s
                    ORDER BY event_id DESC
                    LIMIT 1
                    """,
                    (str(query_id), backend),
                )
                d["cost"] = cost
                require(cost and cost[0] == ("free", "policy_free", 0.0), f"bad local provider cost: {cost}")
                d["requests_seen"] = len(server["seen"])
                require(d["requests_seen"] >= 2, f"expected probe + operator calls, got {server['seen']}")
            finally:
                h.sql("SELECT rvbbit.set_default_provider('openrouter')")


def phase_live_llm(h: E2EHarness) -> None:
    if not LIVE_LLM:
        with h.step("live_llm", "semantic_sql_provider_calls", optional=True):
            raise SkipStep("set RVBBIT_E2E_LIVE_LLM=1 to run paid/provider LLM checks")
        return

    suffix = short_id()
    table = f"public.e2e_live_semantic_{suffix}"
    h.created_tables.append(table)

    with h.step("live_llm", "semantic_sql_provider_calls") as d:
        h.sql(f"CREATE TABLE {table} (id int, title text, observed text) USING rvbbit")
        observations = [
            (
                1,
                "late shipment refund request",
                f"{h.run_id}: Acme reported a late shipment in Denver and asked for a refund.",
            ),
            (
                2,
                "renewal risk after outage",
                f"{h.run_id}: Beta Health said an overnight outage delayed their renewal approval.",
            ),
            (
                3,
                "upgrade interest from analytics team",
                f"{h.run_id}: Cedar Analytics asked about a larger plan after a successful pilot.",
            ),
            (
                4,
                "field note about damaged package",
                f"{h.run_id}: Delta Retail saw damaged packaging and requested replacement tracking.",
            ),
            (
                5,
                "customer success escalation",
                f"{h.run_id}: Eon Labs escalated a support case involving missing invoices.",
            ),
        ][:LIVE_ROWS]
        for row in observations:
            h.sql(f"INSERT INTO {table} VALUES (%s, %s, %s)", row)
        h.sql(f"ANALYZE {table}")
        h.sql(f"SELECT rvbbit.export_to_parquet('{table}'::regclass)")
        h.sql("SELECT rvbbit.flush_cache()")
        query_id = h.scalar("SELECT rvbbit.reset_query_id()")
        d["query_id"] = str(query_id)

        rows = h.sql(
            f"""
            SELECT id, rvbbit.summarize(observed) AS summary
            FROM {table}
            ORDER BY id
            LIMIT {LIVE_ROWS}
            """
        )
        d["rows"] = rows
        require(len(rows) == LIVE_ROWS, f"expected {LIVE_ROWS} summarized rows, got {rows}")
        require(
            all(isinstance(row[1], str) and row[1].strip() for row in rows),
            f"empty semantic summary row: {rows}",
        )

        receipt_rows = h.sql(
            """
            SELECT receipt_id, model, jsonb_array_length(coalesce(sub_calls, '[]'::jsonb)) AS sub_calls,
                   error
            FROM rvbbit.receipts
            WHERE operator = 'summarize'
              AND query_id = %s::uuid
            ORDER BY invocation_at
            """,
            (str(query_id),),
        )
        d["receipts"] = [
            {
                "receipt_id": str(row[0]),
                "model": row[1],
                "sub_calls": row[2],
                "error": row[3],
            }
            for row in receipt_rows
        ]
        require(len(receipt_rows) == LIVE_ROWS, f"expected {LIVE_ROWS} summarize receipts, got {receipt_rows}")
        require(all(row[2] >= 1 and row[3] is None for row in receipt_rows), f"bad live receipts: {receipt_rows}")

        d["cost_events_backfilled"] = h.scalar("SELECT rvbbit.backfill_cost_events_from_receipts(1000)")
        d["cost_audit"] = h.sql(
            """
            SELECT audit_status, count(*)
            FROM rvbbit.receipt_cost_audit
            WHERE query_id = %s::uuid
              AND operator = 'summarize'
            GROUP BY audit_status
            ORDER BY audit_status
            """,
            (str(query_id),),
        )
        d["cost_events"] = h.scalar(
            "SELECT count(*) FROM rvbbit.cost_events WHERE query_id = %s::uuid",
            (str(query_id),),
        )
        require(d["cost_events"] >= LIVE_ROWS, f"expected cost events for live semantic calls: {d}")

    with h.step("live_llm", "openai_direct_provider_calls") as d:
        suffix = short_id()
        backend = f"e2e_openai_{suffix}"
        op = f"e2e_openai_direct_{suffix}"
        h.created_backends.append(backend)
        h.created_ops.append(op)
        d["model"] = OPENAI_LIVE_MODEL
        rate = h.sql(
            """
            SELECT input_per_mtok::float8, output_per_mtok::float8
            FROM rvbbit.model_rates
            WHERE model = %s
            """,
            (OPENAI_LIVE_MODEL,),
        )
        d["model_rate"] = rate
        require(rate, f"missing rvbbit.model_rates row for direct OpenAI model {OPENAI_LIVE_MODEL!r}")
        h.sql(
            """
            SELECT rvbbit.register_backend(
              backend_name => %s,
              backend_endpoint => 'https://api.openai.com/v1/chat/completions',
              backend_transport => 'openai_chat',
              backend_max_concur => 2,
              backend_timeout_ms => 120000,
              backend_auth_env => 'OPENAI_API_KEY',
              backend_opts => '{"max_tokens_field":"max_completion_tokens"}'::jsonb,
              backend_description => 'E2E direct OpenAI provider'
            )
            """,
            (backend,),
        )
        h.sql("SELECT rvbbit.reload_backends()")
        h.sql(
            """
            SELECT rvbbit.create_operator(
              op_name => %s,
              op_arg_names => ARRAY['text'],
              op_return_type => 'text',
              op_steps => %s::jsonb
            )
            """,
            (
                op,
                json.dumps(
                    [
                        {
                            "name": "ask",
                            "kind": "llm",
                            "provider": backend,
                            "model": OPENAI_LIVE_MODEL,
                            "system": "Reply with exactly one lowercase word.",
                            "user": "What color is {{ inputs.text }}?",
                            "max_tokens": 16,
                            "temperature": 0,
                        }
                    ]
                ),
            ),
        )
        h.sql("SELECT rvbbit.flush_cache()")
        query_id = h.scalar("SELECT rvbbit.reset_query_id()")
        d["query_id"] = str(query_id)
        out = h.scalar(f"SELECT rvbbit.{op}('fresh grass')")
        d["output"] = out
        require(isinstance(out, str) and out.strip(), f"direct OpenAI provider returned empty output: {d}")
        receipts = h.sql(
            """
            SELECT receipt_id, model, error,
                   sub_calls->0->>'backend',
                   sub_calls->0->>'transport',
                   (sub_calls->0->>'tokens_in')::int,
                   (sub_calls->0->>'tokens_out')::int
            FROM rvbbit.receipts
            WHERE operator = %s
              AND query_id = %s::uuid
            ORDER BY invocation_at DESC
            LIMIT 1
            """,
            (op, str(query_id)),
        )
        d["receipts"] = [
            {
                "receipt_id": str(row[0]),
                "model": row[1],
                "error": row[2],
                "backend": row[3],
                "transport": row[4],
                "tokens_in": row[5],
                "tokens_out": row[6],
            }
            for row in receipts
        ]
        require(receipts and receipts[0][2] is None, f"bad direct OpenAI receipt: {d}")
        require(receipts[0][3] == backend and receipts[0][4] == "openai_chat", f"wrong provider route: {d}")
        require((receipts[0][5] or 0) > 0 and (receipts[0][6] or 0) > 0, f"missing usage tokens: {d}")
        d["cost_events_backfilled"] = h.scalar("SELECT rvbbit.backfill_cost_events_from_receipts(1000)")
        costs = h.sql(
            """
            SELECT status, cost_source, cost_usd::float8, tokens_in, tokens_out
            FROM rvbbit.cost_latest
            WHERE query_id = %s::uuid
              AND backend = %s
            ORDER BY event_id DESC
            LIMIT 1
            """,
            (str(query_id), backend),
        )
        d["costs"] = costs
        require(costs, f"missing direct OpenAI cost event: {d}")
        require(costs[0][0] == "estimated" and costs[0][1] == "model_rate", f"expected model-rate estimate: {d}")
        require((costs[0][2] or 0.0) > 0.0, f"expected positive direct OpenAI estimated cost: {d}")

    with h.step("live_llm", "anthropic_direct_provider_calls") as d:
        suffix = short_id()
        backend = f"e2e_anthropic_{suffix}"
        op = f"e2e_anthropic_direct_{suffix}"
        h.created_backends.append(backend)
        h.created_ops.append(op)
        d["model"] = ANTHROPIC_LIVE_MODEL
        rate = h.sql(
            """
            SELECT input_per_mtok::float8, output_per_mtok::float8
            FROM rvbbit.model_rates
            WHERE model = %s
            """,
            (ANTHROPIC_LIVE_MODEL,),
        )
        d["model_rate"] = rate
        require(rate, f"missing rvbbit.model_rates row for Anthropic model {ANTHROPIC_LIVE_MODEL!r}")
        h.sql(
            """
            SELECT rvbbit.register_backend(
              backend_name => %s,
              backend_endpoint => 'https://api.anthropic.com/v1/messages',
              backend_transport => 'anthropic',
              backend_max_concur => 2,
              backend_timeout_ms => 120000,
              backend_auth_env => 'ANTHROPIC_API_KEY',
              backend_description => 'E2E direct Anthropic provider'
            )
            """,
            (backend,),
        )
        h.sql("SELECT rvbbit.reload_backends()")
        h.sql(
            """
            SELECT rvbbit.create_operator(
              op_name => %s,
              op_arg_names => ARRAY['text'],
              op_return_type => 'text',
              op_steps => %s::jsonb
            )
            """,
            (
                op,
                json.dumps(
                    [
                        {
                            "name": "ask",
                            "kind": "llm",
                            "provider": backend,
                            "model": ANTHROPIC_LIVE_MODEL,
                            "system": "Reply with exactly one lowercase word.",
                            "user": "What color is {{ inputs.text }}?",
                            "max_tokens": 16,
                            "temperature": 0,
                        }
                    ]
                ),
            ),
        )
        h.sql("SELECT rvbbit.flush_cache()")
        query_id = h.scalar("SELECT rvbbit.reset_query_id()")
        d["query_id"] = str(query_id)
        out = h.scalar(f"SELECT rvbbit.{op}('fresh grass')")
        d["output"] = out
        require(isinstance(out, str) and out.strip(), f"direct Anthropic provider returned empty output: {d}")
        receipts = h.sql(
            """
            SELECT receipt_id, model, error,
                   sub_calls->0->>'backend',
                   sub_calls->0->>'transport',
                   (sub_calls->0->>'tokens_in')::int,
                   (sub_calls->0->>'tokens_out')::int
            FROM rvbbit.receipts
            WHERE operator = %s
              AND query_id = %s::uuid
            ORDER BY invocation_at DESC
            LIMIT 1
            """,
            (op, str(query_id)),
        )
        d["receipts"] = [
            {
                "receipt_id": str(row[0]),
                "model": row[1],
                "error": row[2],
                "backend": row[3],
                "transport": row[4],
                "tokens_in": row[5],
                "tokens_out": row[6],
            }
            for row in receipts
        ]
        require(receipts and receipts[0][2] is None, f"bad direct Anthropic receipt: {d}")
        require(receipts[0][3] == backend and receipts[0][4] == "anthropic", f"wrong provider route: {d}")
        require((receipts[0][5] or 0) > 0 and (receipts[0][6] or 0) > 0, f"missing usage tokens: {d}")
        d["cost_events_backfilled"] = h.scalar("SELECT rvbbit.backfill_cost_events_from_receipts(1000)")
        costs = h.sql(
            """
            SELECT status, cost_source, cost_usd::float8, tokens_in, tokens_out
            FROM rvbbit.cost_latest
            WHERE query_id = %s::uuid
              AND backend = %s
            ORDER BY event_id DESC
            LIMIT 1
            """,
            (str(query_id), backend),
        )
        d["costs"] = costs
        require(costs, f"missing direct Anthropic cost event: {d}")
        require(costs[0][0] == "estimated" and costs[0][1] == "model_rate", f"expected model-rate estimate: {d}")
        require((costs[0][2] or 0.0) > 0.0, f"expected positive direct Anthropic estimated cost: {d}")

    with h.step("live_llm", "gemini_api_key_provider_calls") as d:
        suffix = short_id()
        backend = f"e2e_gemini_key_{suffix}"
        op = f"e2e_gemini_key_{suffix}"
        h.created_backends.append(backend)
        h.created_ops.append(op)
        d["model"] = GEMINI_LIVE_MODEL
        rate = h.sql(
            """
            SELECT input_per_mtok::float8, output_per_mtok::float8
            FROM rvbbit.model_rates
            WHERE model = %s
            """,
            (GEMINI_LIVE_MODEL,),
        )
        d["model_rate"] = rate
        require(rate, f"missing rvbbit.model_rates row for Gemini model {GEMINI_LIVE_MODEL!r}")
        h.sql(
            """
            SELECT rvbbit.register_backend(
              backend_name => %s,
              backend_endpoint => 'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent',
              backend_transport => 'gemini',
              backend_max_concur => 2,
              backend_timeout_ms => 120000,
              backend_auth_env => 'GEMINI_API_KEY',
              backend_description => 'E2E direct Gemini API-key provider'
            )
            """,
            (backend,),
        )
        h.sql("SELECT rvbbit.reload_backends()")
        h.sql(
            """
            SELECT rvbbit.create_operator(
              op_name => %s,
              op_arg_names => ARRAY['text'],
              op_return_type => 'text',
              op_steps => %s::jsonb
            )
            """,
            (
                op,
                json.dumps(
                    [
                        {
                            "name": "ask",
                            "kind": "llm",
                            "provider": backend,
                            "model": GEMINI_LIVE_MODEL,
                            "system": "Reply with exactly one lowercase word.",
                            "user": "What color is {{ inputs.text }}?",
                            "max_tokens": 16,
                            "temperature": 0,
                        }
                    ]
                ),
            ),
        )
        h.sql("SELECT rvbbit.flush_cache()")
        query_id = h.scalar("SELECT rvbbit.reset_query_id()")
        d["query_id"] = str(query_id)
        out = h.scalar(f"SELECT rvbbit.{op}('fresh grass')")
        d["output"] = out
        require(isinstance(out, str) and out.strip(), f"direct Gemini API-key provider returned empty output: {d}")
        receipts = h.sql(
            """
            SELECT receipt_id, model, error,
                   sub_calls->0->>'backend',
                   sub_calls->0->>'transport',
                   (sub_calls->0->>'tokens_in')::int,
                   (sub_calls->0->>'tokens_out')::int
            FROM rvbbit.receipts
            WHERE operator = %s
              AND query_id = %s::uuid
            ORDER BY invocation_at DESC
            LIMIT 1
            """,
            (op, str(query_id)),
        )
        d["receipts"] = [
            {
                "receipt_id": str(row[0]),
                "model": row[1],
                "error": row[2],
                "backend": row[3],
                "transport": row[4],
                "tokens_in": row[5],
                "tokens_out": row[6],
            }
            for row in receipts
        ]
        require(receipts and receipts[0][2] is None, f"bad direct Gemini API-key receipt: {d}")
        require(receipts[0][3] == backend and receipts[0][4] == "gemini", f"wrong provider route: {d}")
        require((receipts[0][5] or 0) > 0 and (receipts[0][6] or 0) > 0, f"missing usage tokens: {d}")
        d["cost_events_backfilled"] = h.scalar("SELECT rvbbit.backfill_cost_events_from_receipts(1000)")
        costs = h.sql(
            """
            SELECT status, cost_source, cost_usd::float8, tokens_in, tokens_out
            FROM rvbbit.cost_latest
            WHERE query_id = %s::uuid
              AND backend = %s
            ORDER BY event_id DESC
            LIMIT 1
            """,
            (str(query_id), backend),
        )
        d["costs"] = costs
        require(costs, f"missing direct Gemini API-key cost event: {d}")
        require(costs[0][0] == "estimated" and costs[0][1] == "model_rate", f"expected model-rate estimate: {d}")
        require((costs[0][2] or 0.0) > 0.0, f"expected positive direct Gemini API-key estimated cost: {d}")

    with h.step("live_llm", "gemini_adc_provider_calls") as d:
        if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
            raise SkipStep("GOOGLE_APPLICATION_CREDENTIALS not present in bench container")
        suffix = short_id()
        backend = f"e2e_gemini_adc_{suffix}"
        op = f"e2e_gemini_adc_{suffix}"
        h.created_backends.append(backend)
        h.created_ops.append(op)
        d["model"] = GEMINI_LIVE_MODEL
        rate = h.sql(
            """
            SELECT input_per_mtok::float8, output_per_mtok::float8
            FROM rvbbit.model_rates
            WHERE model = %s
            """,
            (GEMINI_LIVE_MODEL,),
        )
        d["model_rate"] = rate
        require(rate, f"missing rvbbit.model_rates row for Gemini model {GEMINI_LIVE_MODEL!r}")
        h.sql(
            """
            SELECT rvbbit.register_backend(
              backend_name => %s,
              backend_endpoint => 'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent',
              backend_transport => 'gemini',
              backend_max_concur => 2,
              backend_timeout_ms => 120000,
              backend_auth_env => 'GOOGLE_APPLICATION_CREDENTIALS',
              backend_opts => '{"auth_mode":"google_adc"}'::jsonb,
              backend_description => 'E2E direct Gemini ADC provider'
            )
            """,
            (backend,),
        )
        h.sql("SELECT rvbbit.reload_backends()")
        h.sql(
            """
            SELECT rvbbit.create_operator(
              op_name => %s,
              op_arg_names => ARRAY['text'],
              op_return_type => 'text',
              op_steps => %s::jsonb
            )
            """,
            (
                op,
                json.dumps(
                    [
                        {
                            "name": "ask",
                            "kind": "llm",
                            "provider": backend,
                            "model": GEMINI_LIVE_MODEL,
                            "system": "Reply with exactly one lowercase word.",
                            "user": "What color is {{ inputs.text }}?",
                            "max_tokens": 16,
                            "temperature": 0,
                        }
                    ]
                ),
            ),
        )
        h.sql("SELECT rvbbit.flush_cache()")
        query_id = h.scalar("SELECT rvbbit.reset_query_id()")
        d["query_id"] = str(query_id)
        out = h.scalar(f"SELECT rvbbit.{op}('fresh grass')")
        d["output"] = out
        require(isinstance(out, str) and out.strip(), f"direct Gemini ADC provider returned empty output: {d}")
        receipts = h.sql(
            """
            SELECT receipt_id, model, error,
                   sub_calls->0->>'backend',
                   sub_calls->0->>'transport',
                   (sub_calls->0->>'tokens_in')::int,
                   (sub_calls->0->>'tokens_out')::int
            FROM rvbbit.receipts
            WHERE operator = %s
              AND query_id = %s::uuid
            ORDER BY invocation_at DESC
            LIMIT 1
            """,
            (op, str(query_id)),
        )
        d["receipts"] = [
            {
                "receipt_id": str(row[0]),
                "model": row[1],
                "error": row[2],
                "backend": row[3],
                "transport": row[4],
                "tokens_in": row[5],
                "tokens_out": row[6],
            }
            for row in receipts
        ]
        require(receipts and receipts[0][2] is None, f"bad direct Gemini ADC receipt: {d}")
        require(receipts[0][3] == backend and receipts[0][4] == "gemini", f"wrong provider route: {d}")
        require((receipts[0][5] or 0) > 0 and (receipts[0][6] or 0) > 0, f"missing usage tokens: {d}")
        d["cost_events_backfilled"] = h.scalar("SELECT rvbbit.backfill_cost_events_from_receipts(1000)")
        costs = h.sql(
            """
            SELECT status, cost_source, cost_usd::float8, tokens_in, tokens_out
            FROM rvbbit.cost_latest
            WHERE query_id = %s::uuid
              AND backend = %s
            ORDER BY event_id DESC
            LIMIT 1
            """,
            (str(query_id), backend),
        )
        d["costs"] = costs
        require(costs, f"missing direct Gemini ADC cost event: {d}")
        require(costs[0][0] == "estimated" and costs[0][1] == "model_rate", f"expected model-rate estimate: {d}")
        require((costs[0][2] or 0.0) > 0.0, f"expected positive direct Gemini ADC estimated cost: {d}")

    with h.step("live_llm", "triples_provider_shape") as d:
        h.sql("SELECT rvbbit.flush_cache()")
        query_id = h.scalar("SELECT rvbbit.reset_query_id()")
        text = (
            f"{h.run_id}: Ranger Alice reported a night road crossing near Willow Creek. "
            "The witness saw two tall figures beside a damaged trail camera, and "
            "the county sheriff collected footprints the next morning."
        )
        rows = h.sql(
            """
            SELECT subject_kind, subject, predicate, object_kind, object, confidence, evidence
            FROM rvbbit.triples_rows(%s, 'people, places, events, evidence', '{}'::jsonb)
            LIMIT 8
            """,
            (text,),
        )
        d["query_id"] = str(query_id)
        d["rows"] = rows
        require(len(rows) >= 1, f"live triples returned no rows: {rows}")
        receipt = h.sql(
            """
            SELECT receipt_id, model, error, jsonb_array_length(coalesce(sub_calls, '[]'::jsonb))
            FROM rvbbit.receipts
            WHERE operator = 'triples'
              AND query_id = %s::uuid
            ORDER BY invocation_at DESC
            LIMIT 1
            """,
            (str(query_id),),
        )
        d["recent_triples_receipt_for_query"] = [
            {
                "receipt_id": str(row[0]),
                "model": row[1],
                "error": row[2],
                "sub_calls": row[3],
            }
            for row in receipt
        ]
        require(receipt and receipt[0][2] is None and receipt[0][3] >= 1, f"missing/bad triples receipt: {receipt}")


def main() -> int:
    h = E2EHarness()
    print(f"rvbbit acceptance run: {h.run_id}")
    print(f"mode: {h.mode}")
    print(f"artifacts: {h.artifact_dir}\n")
    try:
        phase_catalog(h)
        phase_provider_catalogs(h)
        imported = phase_imports(h)
        storage_table = phase_storage_and_routing(h)
        phase_reload_persistence(h, storage_table)
        phase_dump_restore(h)
        phase_route_training(h)
        phase_model_training(h, imported)
        semantic_op = phase_semantic_echo(h)
        stress_op = phase_semantic_stress(h)
        phase_backend_failure_audit(h)
        phase_embeddings(h, imported)
        phase_python(h)
        phase_mcp(h)
        phase_kg(h)
        phase_kg_imported_text(h, imported)
        phase_warren(h)
        phase_costs(h, stress_op or semantic_op)
        phase_diagnostics(h)
        phase_live_llm(h)
        return h.finish()
    finally:
        h.close()


if __name__ == "__main__":
    sys.exit(main())
