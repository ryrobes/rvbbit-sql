#!/usr/bin/env python3
"""Exercise a fresh hosted appliance through the real Calliope setup API.

The script intentionally keeps login and source credentials in memory. It
prints only credential-free health, lineage, and mirror-run receipts.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Fixture:
    connection_name: str
    label: str
    dialect: str
    source_dsn: str
    schemas: tuple[tuple[str, str], ...]


FIXTURES = (
    Fixture(
        "fixture_postgres",
        "Fixture PostgreSQL commerce",
        "postgresql",
        "postgresql://mirror_reader:mirror_readonly@sample-postgres:5432/commerce",
        (("sales", "commerce_sales_pg"), ("support", "commerce_support_pg")),
    ),
    Fixture(
        "fixture_mysql",
        "Fixture MySQL CRM",
        "mysql",
        "mysql+pymysql://mirror_reader:mirror_readonly@sample-mysql:3306/crm",
        (("crm", "crm_mysql"),),
    ),
    Fixture(
        "fixture_mssql",
        "Fixture SQL Server operations",
        "mssql",
        "mssql+pymssql://mirror_reader:Rvbbit_Mirror_Readonly_2026%21@sample-mssql:1433/operations",
        (("field_ops", "operations_mssql"),),
    ),
    Fixture(
        "fixture_oracle",
        "Fixture Oracle Customer Orders and Sales History",
        "oracle",
        "oracle+oracledb://mirror_reader:Rvbbit_Oracle_Readonly_2026%21@sample-oracle:1521/?service_name=FREEPDB1",
        (("CO", "oracle_co"), ("SH", "oracle_sh")),
    ),
)


class ApplianceClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(self.cookies),
        )

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
        expect_json: bool = True,
    ) -> Any:
        body = None
        request_headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers=request_headers,
            method=method,
        )
        try:
            with self.opener.open(request, timeout=120) as response:
                raw = response.read()
                if not expect_json:
                    return {"status": response.status, "url": response.url}
                return json.loads(raw or b"{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                detail = json.loads(raw or b"{}")
            except Exception:
                detail = {"status": exc.code}
            raise RuntimeError(
                f"{method} {path} failed with HTTP {exc.code}: "
                f"{json.dumps(detail, separators=(',', ':'))[:1200]}"
            ) from None
        except urllib.error.URLError:
            raise RuntimeError(f"{method} {path} could not reach the appliance") from None

    def login(self, email: str, password: str) -> None:
        form = urllib.parse.urlencode(
            {"email": email, "password": password, "next": "/calliope/setup"}
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/login",
            data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                response.read(1024)
        except (urllib.error.HTTPError, urllib.error.URLError):
            raise RuntimeError("Appliance login failed") from None
        if not any(cookie.name == "wh_session" for cookie in self.cookies):
            raise RuntimeError("Appliance login did not issue a session cookie")


def _print(label: str, value: Any) -> None:
    print(
        f"{label}: {json.dumps(value, sort_keys=True, separators=(',', ':'))}",
        flush=True,
    )


def _table_plan(table: dict[str, Any]) -> dict[str, Any]:
    source_name = str(table.get("name") or "")
    column_names = {
        str(column.get("name") or "")
        for column in table.get("columns") or []
        if isinstance(column, dict)
    }
    primary_key = [str(value) for value in table.get("primary_key") or []]
    incremental = bool(primary_key and "updated_at" in column_names)
    return {
        "source_table": source_name,
        "destination_table": str(table.get("destination_table") or source_name),
        "load_mode": "incremental_upsert" if incremental else "snapshot",
        "primary_key": primary_key or None,
        "cursor_column": "updated_at" if incremental else None,
        "included_columns": None,
    }


def configure_profile(
    client: ApplianceClient, headers: dict[str, str]
) -> None:
    current = client.request("GET", "/api/calliope/setup/company")
    profile = current.get("profile") or {}
    proposal = {
        "expected_profile_version": int(profile.get("version") or 0),
        "company_name": "Hosted Appliance Fixture Company",
        "summary": "A disposable company profile used to validate clean first-boot behavior.",
        "timezone": "America/New_York",
        "fiscal_year_start_month": 1,
        "week_starts_on": "monday",
        "reporting_calendar_notes": "Test-only calendar.",
        "terminology": [
            {"term": "work order", "meaning": "A field-service job in the operations source."},
            {"term": "account", "meaning": "A customer represented across commerce and CRM sources."},
        ],
        "business_questions": [
            "Which customers have both open support work and recent orders?",
            "Which field work orders are consuming more hours than estimated?",
        ],
    }
    review = client.request(
        "POST", "/api/calliope/setup/company/plan", proposal, headers=headers
    )
    applied = client.request(
        "POST",
        "/api/calliope/setup/company/apply",
        {"plan": review["plan"], "plan_token": review["plan_token"]},
        headers=headers,
    )
    _print(
        "company_profile",
        {
            "name": applied["profile"].get("company_name"),
            "version": applied["profile"].get("version"),
            "status": applied["profile"].get("status"),
        },
    )


def configure_fixtures(
    client: ApplianceClient,
    headers: dict[str, str],
    fixtures: tuple[Fixture, ...] = FIXTURES,
) -> set[str]:
    job_names: set[str] = set()
    for fixture in fixtures:
        saved = client.request(
            "POST",
            "/api/calliope/setup/databases",
            {
                "connection_name": fixture.connection_name,
                "label": fixture.label,
                "dialect": fixture.dialect,
                "environment": "disposable fixture",
                "source_dsn": fixture.source_dsn,
            },
            headers=headers,
        )
        _print(
            "credential",
            {
                "connection": saved.get("connection_name"),
                "reference": saved.get("credential_ref"),
                "secret_persisted_in_calliope": saved.get(
                    "secret_persisted_in_calliope"
                ),
            },
        )
        probe = client.request(
            "POST",
            f"/api/calliope/setup/databases/{fixture.connection_name}/probe",
            {},
            headers=headers,
        )
        _print(
            "probe",
            {
                "connection": fixture.connection_name,
                "ok": probe.get("probe", {}).get("ok"),
                "elapsed_ms": probe.get("probe", {}).get("elapsed_ms"),
            },
        )
        for source_schema, destination_schema in fixture.schemas:
            discovered = client.request(
                "POST",
                f"/api/calliope/setup/databases/{fixture.connection_name}/discover",
                {"source_schema": source_schema, "include_views": True, "limit": 200},
                headers=headers,
            )
            tables = discovered.get("discovery", {}).get("tables") or []
            if not tables:
                raise RuntimeError(
                    f"No tables discovered for {fixture.connection_name}.{source_schema}"
                )
            proposal = {
                "source_schema": source_schema,
                "destination_schema": destination_schema,
                "schedule_seconds": None,
                "run_now": True,
                "tables": [_table_plan(table) for table in tables],
            }
            reviewed = client.request(
                "POST",
                f"/api/calliope/setup/databases/{fixture.connection_name}/plan",
                proposal,
                headers=headers,
            )
            applied = client.request(
                "POST",
                f"/api/calliope/setup/databases/{fixture.connection_name}/apply",
                {
                    "plan": reviewed["plan"],
                    "plan_token": reviewed["plan_token"],
                },
                headers=headers,
            )
            job_name = str(applied["plan"]["job_name"])
            job_names.add(job_name)
            _print(
                "mirror_queued",
                {
                    "job": job_name,
                    "source_schema": source_schema,
                    "destination_schema": destination_schema,
                    "table_count": len(tables),
                    "run_id": applied.get("run_id"),
                },
            )
    return job_names


def wait_for_mirrors(
    client: ApplianceClient,
    expected_jobs: set[str],
    timeout_seconds: int,
) -> dict[str, dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    latest: dict[str, dict[str, Any]] = {}
    while time.monotonic() < deadline:
        snapshot = client.request("GET", "/api/calliope/setup/databases")
        latest = {
            str(job.get("job_name")): job
            for connection in snapshot.get("connections") or []
            for job in connection.get("jobs") or []
            if job.get("job_name") in expected_jobs
        }
        failures = {
            name: job
            for name, job in latest.items()
            if job.get("latest_run_status") in {"failed", "cancelled", "partial"}
        }
        if failures:
            safe = {
                name: {
                    "status": job.get("latest_run_status"),
                    "error_code": job.get("latest_run_error_code"),
                    "error": job.get("latest_run_error_message"),
                }
                for name, job in failures.items()
            }
            raise RuntimeError(f"Mirror jobs failed: {json.dumps(safe, sort_keys=True)}")
        # Mirror runs use the durable control-plane vocabulary ("succeeded"),
        # while older callers may still expose "completed".  Accept either at
        # this API boundary, but treat a partial load as a failed smoke test.
        if expected_jobs and all(
            latest.get(name, {}).get("latest_run_status")
            in {"succeeded", "completed"}
            for name in expected_jobs
        ):
            return latest
        time.sleep(3)
    states = {
        name: latest.get(name, {}).get("latest_run_status") for name in expected_jobs
    }
    raise RuntimeError(f"Mirror jobs did not finish within {timeout_seconds}s: {states}")


def execute_admin_action(
    client: ApplianceClient, action_id: str, inputs: dict[str, Any]
) -> dict[str, Any]:
    encoded = urllib.parse.quote(action_id, safe="")
    current = client.request("GET", f"/api/calliope/actions/{encoded}")
    if current.get("action", {}).get("id") != action_id:
        raise RuntimeError(f"Calliope did not resolve admin action {action_id}")
    planned = client.request(
        "POST", f"/api/calliope/actions/{encoded}/plan", {"inputs": inputs}
    )
    run_id = str(planned.get("run", {}).get("id") or "")
    if not run_id:
        raise RuntimeError(f"Calliope did not create a plan for {action_id}")
    executed = client.request(
        "POST", f"/api/calliope/action-runs/{run_id}/execute", {"inputs": {}}
    )
    run = executed.get("run") or {}
    if run.get("status") != "complete":
        raise RuntimeError(f"Calliope admin action {action_id} did not complete")
    return run


def exercise_mirror_administration(
    client: ApplianceClient, job_name: str, timeout_seconds: int
) -> None:
    action_id = f"mirror.manage:{job_name}"
    paused = execute_admin_action(client, action_id, {"operation": "pause"})
    if paused.get("verification", {}).get("enabled") is not False:
        raise RuntimeError("Mirror pause did not persist")
    resumed = execute_admin_action(client, action_id, {"operation": "resume"})
    if resumed.get("verification", {}).get("enabled") is not True:
        raise RuntimeError("Mirror resume did not persist")
    queued = execute_admin_action(client, action_id, {"operation": "run_now"})
    queued_run_id = queued.get("result", {}).get("queued_run_id")
    if not queued_run_id:
        raise RuntimeError("Mirror run-now action did not create a durable run")
    completed = wait_for_mirrors(client, {job_name}, timeout_seconds)[job_name]
    _print(
        "mirror_admin",
        {
            "job": job_name,
            "pause": "complete",
            "resume": "complete",
            "run_now": "succeeded",
            "run_id": completed.get("run_id"),
            "rows_loaded": completed.get("latest_run_rows_loaded"),
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--email", default="ryan@rvbbit.ai")
    parser.add_argument("--password-env", default="HOSTED_SMOKE_PASSWORD")
    parser.add_argument("--wait-seconds", type=int, default=1800)
    parser.add_argument("--skip-fixtures", action="store_true")
    parser.add_argument("--skip-profile", action="store_true")
    parser.add_argument(
        "--fixture",
        action="append",
        choices=tuple(fixture.connection_name for fixture in FIXTURES),
        help="Run only the named fixture; repeat to select more than one.",
    )
    args = parser.parse_args()

    password = os.environ.get(args.password_env, "")
    if not password:
        parser.error(f"{args.password_env} must contain the appliance login password")

    client = ApplianceClient(args.base_url)
    health = client.request("GET", "/health", expect_json=False)
    if health.get("status") != 200:
        raise RuntimeError("The appliance health endpoint did not return HTTP 200")
    _print("health", health)
    client.login(args.email, password)
    setup = client.request("POST", "/api/calliope/setup", {})
    if not setup.get("can_manage") or not setup.get("mutation_token"):
        raise RuntimeError("The smoke-test identity is not an appliance administrator")
    headers = {"X-Calliope-Setup-Token": str(setup["mutation_token"])}
    _print(
        "setup",
        {
            "can_manage": setup.get("can_manage"),
            "launched": setup.get("launched"),
            "ready_count": setup.get("summary", {}).get("ready"),
            "required_count": setup.get("summary", {}).get("required"),
        },
    )
    preflight = client.request(
        "POST", "/api/calliope/setup/preflight", {}, headers=headers
    )
    checks = preflight.get("preflight", {}).get("checks") or []
    _print(
        "preflight",
        {
            "ready": preflight.get("preflight", {}).get("ready"),
            "checks": [
                {"id": check.get("id"), "status": check.get("status")}
                for check in checks
            ],
        },
    )
    if not args.skip_profile:
        configure_profile(client, headers)
    if not args.skip_fixtures:
        selected = tuple(
            fixture
            for fixture in FIXTURES
            if not args.fixture or fixture.connection_name in args.fixture
        )
        jobs = configure_fixtures(client, headers, selected)
        completed = wait_for_mirrors(client, jobs, max(30, args.wait_seconds))
        _print(
            "mirrors_completed",
            {
                name: {
                    "status": job.get("latest_run_status"),
                    "rows_loaded": job.get("latest_run_rows_loaded"),
                    "table_count": job.get("table_count"),
                }
                for name, job in sorted(completed.items())
            },
        )
        exercise_mirror_administration(
            client, sorted(jobs)[0], max(30, args.wait_seconds)
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
