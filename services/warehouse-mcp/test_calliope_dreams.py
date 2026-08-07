"""Focused contracts for Calliope's evidence-backed Dream loop."""
from __future__ import annotations

import importlib.util
import asyncio
import json
import sys
import types
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest


_HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location(
    "warehouse_calliope_dreams_test_module", _HERE / "calliope_dreams.py"
)
dreams = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = dreams
_SPEC.loader.exec_module(dreams)


def test_signal_redaction_keeps_intent_but_removes_identity_and_credentials():
    value = dreams.redact_signal(
        "Ask Ada@example.com about the renewal. "
        "Authorization: Bearer secret-token-value password=hunter2 "
        "https://example.test/?token=also-secret"
    )

    assert "renewal" in value
    assert "[person]" in value
    assert "ada@example.com" not in value.lower()
    assert "hunter2" not in value
    assert "secret-token-value" not in value
    assert "also-secret" not in value


def test_json_parser_accepts_fenced_model_output_and_rejects_non_documents():
    assert dreams.parse_json_response("```json\n{\"dreams\": []}\n```") == {"dreams": []}
    assert dreams.parse_json_response({"message": {"content": "prefix {\"ok\":true} suffix"}}) == {"ok": True}
    with pytest.raises(ValueError, match="Dream document"):
        dreams.parse_json_response("there is nothing structured here")


def test_observations_must_cite_collected_evidence_and_are_deidentified():
    evidence = {
        "tool:1": {
            "kind": "activity",
            "label": "run_sql · 18 calls",
            "detail": "3 users · 2 errors · web",
        }
    }
    result = dreams.normalize_observations(
        {
            "observations": [
                {
                    "kind": "friction",
                    "title": "A recurring reporting retry",
                    "summary": "The same governed query family was retried after errors across several sessions.",
                    "evidence_ids": ["tool:1", "invented:99"],
                    "entities": ["Revenue reporting"],
                    "signal_count": 4,
                    "confidence": 8,
                },
                {
                    "kind": "gap",
                    "title": "Unsupported hunch",
                    "summary": "This model-authored hunch has no evidence supplied by the collector.",
                    "evidence_ids": ["invented:1"],
                },
            ]
        },
        evidence,
    )

    assert len(result) == 1
    assert result[0]["evidence_ids"] == ["tool:1"]
    assert result[0]["evidence"] == [evidence["tool:1"]]
    assert result[0]["confidence"] == 1.0
    assert "email" not in json.dumps(result[0]).lower()


def test_candidates_require_real_observations_and_keep_a_deep_editorial_reservoir():
    observations = [{
        "id": "observation:1",
        "kind": "repetition",
        "title": "Teams rebuild the same weekly view",
        "summary": "A repeated governed activity pattern.",
    }]
    raw = {
        "dreams": [
            {
                "dream_type": "automation",
                "output_kind": "prototype",
                "problem_key": f"weekly reusable view domain{index}",
                "title": f"A distinct domain{index} opportunity",
                "thesis": f"Domain{index} signal{index} can produce outcome{index} through mechanism{index}, grounded in repeated governed activity.",
                "observation_ids": ["observation:1"],
                "impact": "impossible",
                "effort": "instant",
                "output": {"artifact_type": "dashboard"},
            }
            for index in range(15)
        ] + [{
            "dream_type": "strategic",
            "output_kind": "project_plan",
            "title": "An unsupported project",
            "thesis": "This should be rejected because its observation reference was fabricated.",
            "observation_ids": ["observation:404"],
        }]
    }

    result = dreams.normalize_candidates(raw, observations)

    assert len(result) == dreams.MAX_CANDIDATES == 12
    assert all(item["impact"] == "medium" and item["effort"] == "medium" for item in result)
    assert all(item["observation_ids"] == ["observation:1"] for item in result)
    assert all(item["output"]["implementation_prompt"] for item in result)

    ranked = dreams.rank_candidates(result, observations)
    assert len([item for item in ranked if item["portfolio_state"] == "promoted"]) == dreams.MAX_DREAMS == 3
    assert len([item for item in ranked if item["portfolio_state"] == "backlog"]) == 9
    assert [item["portfolio_rank"] for item in ranked[:3]] == [1, 2, 3]


def test_evidence_lab_admits_only_bounded_observed_queries_and_installed_clover():
    observations = [{"id": "observation:1"}]
    targets = [{
        "relation": "sales.tickets",
        "columns": [
            {"name": "status", "type": "text"},
            {"name": "description", "type": "text", "semantic_text": True},
        ],
    }]
    affordances = [{"operator": "clover_relevance", "runtime_state": "available"}]
    raw = {"probes": [
        {
            "id": "distribution",
            "kind": "sql",
            "observation_ids": ["observation:1"],
            "hypothesis": "Ticket volume spans several active lifecycle states.",
            "falsifier": "Only one lifecycle state has any ticket volume.",
            "purpose": "Check whether a reusable lifecycle view is warranted.",
            "sql": "SELECT status,count(*) AS tickets FROM sales.tickets GROUP BY status LIMIT 8",
        },
        {
            "id": "semantic",
            "kind": "clover",
            "observation_ids": ["observation:1"],
            "hypothesis": "A material share of ticket descriptions concerns access friction.",
            "falsifier": "The bounded sample has low semantic relevance to access friction.",
            "purpose": "Test meaning rather than relying on keywords.",
            "operator": "clover_relevance",
            "input_sql": "SELECT description AS sample_text FROM sales.tickets WHERE description IS NOT NULL LIMIT 12",
            "input_columns": {"t": "sample_text"},
            "arguments": {"criterion": "account access or login friction"},
        },
        {
            "id": "unsafe",
            "kind": "sql",
            "observation_ids": ["observation:1"],
            "hypothesis": "This deliberately unsafe test should never be admitted.",
            "falsifier": "The validator correctly rejects the write.",
            "sql": "DELETE FROM sales.tickets",
        },
    ]}

    probes, rejected = dreams.normalize_probe_plan(
        raw, observations, targets, affordances,
        {"max_probes": 4, "max_clover_probes": 2},
    )

    assert [item["kind"] for item in probes] == ["sql", "clover"]
    assert probes[1]["operator"] == "clover_relevance"
    assert len(probes[1]["sql_sha256"]) == 64
    assert rejected == [{"id": "unsafe", "reason": "probe SQL must be one SELECT query"}]


def test_evidence_lab_rejects_raw_narrative_identity_and_unobserved_relations():
    targets = [{
        "relation": "sales.tickets",
        "columns": [
            {"name": "description", "type": "text"},
            {"name": "owner_email", "type": "text"},
        ],
    }]
    with pytest.raises(ValueError, match="sensitive column description"):
        dreams.validate_probe_sql(
            "SELECT description,count(*) FROM sales.tickets GROUP BY description",
            targets,
            mode="aggregate",
        )
    with pytest.raises(ValueError, match="identity column owner_email"):
        dreams.validate_probe_sql(
            "SELECT owner_email AS sample_text FROM sales.tickets LIMIT 8",
            targets,
            mode="sample",
        )
    with pytest.raises(ValueError, match="outside the observed target inventory"):
        dreams.validate_probe_sql(
            "SELECT status,count(*) FROM finance.ledger GROUP BY status",
            targets,
            mode="aggregate",
        )
    with pytest.raises(ValueError, match="outside the exposed target inventory"):
        dreams.validate_probe_sql(
            "SELECT guessed_private_value,count(*) FROM sales.tickets GROUP BY guessed_private_value",
            targets,
            mode="aggregate",
        )
    with pytest.raises(ValueError, match="not in the safe SQL subset"):
        dreams.validate_probe_sql(
            "SELECT rvbbit.clover_relevance(description,'risk'),count(*) FROM sales.tickets",
            targets,
            mode="aggregate",
        )


def test_clover_inputs_are_ephemeral_and_only_aggregate_results_survive(monkeypatch):
    seen = []

    def fake_invoke(_factory, operator, arguments):
        seen.append((operator, arguments))
        return 0.8 if "first" in arguments[0] else 0.2

    monkeypatch.setattr(dreams, "_invoke_clover", fake_invoke)
    probe = {
        "operator": "clover_relevance",
        "input_columns": {"t": "sample_text"},
        "arguments": {"criterion": "renewal risk"},
    }
    result = dreams._clover_probe_result(
        lambda: None,
        probe,
        [
            {"sample_text": "first private narrative ada@example.com"},
            {"sample_text": "second private narrative password=hunter2"},
        ],
    )

    assert result == {
        "sample_size": 2,
        "result_kind": "numeric",
        "mean": 0.5,
        "minimum": 0.2,
        "maximum": 0.8,
    }
    assert "ada@example.com" not in json.dumps(seen).lower()
    assert "hunter2" not in json.dumps(seen).lower()
    assert "private narrative" not in json.dumps(result).lower()


def test_probe_assessor_cannot_upgrade_failed_or_empty_execution():
    receipts = [
        {"id": "probe:1", "execution_status": "complete", "row_count": 3},
        {"id": "probe:2", "execution_status": "complete", "row_count": 0},
        {"id": "probe:3", "execution_status": "error", "row_count": 9},
    ]
    dreams.apply_probe_assessments({"assessments": [
        {"probe_id": "probe:1", "verdict": "supported", "summary": "Three groups supported the pattern."},
        {"probe_id": "probe:2", "verdict": "supported", "summary": "Invented."},
        {"probe_id": "probe:3", "verdict": "supported", "summary": "Invented."},
    ]}, receipts)

    assert receipts[0]["verdict"] == "supported"
    assert receipts[1]["verdict"] == "untested"
    assert receipts[2]["verdict"] == "untested"


def test_context_budget_preserves_every_source_family_instead_of_only_chat():
    signals = [
        {"id": f"recent:chat:{index}", "kind": "conversation", "text": "busy conversation " * 45}
        for index in range(300)
    ]
    signals += [
        {"id": "recent:tool:1", "kind": "tool_pattern", "tool": "run_sql"},
        {"id": "recent:metric:1", "kind": "metric_pattern", "metric": "revenue"},
        {"id": "recent:graph:1", "kind": "graph_pattern", "predicate": "supports"},
        {"id": "recent:knowledge:1", "kind": "knowledge_source", "source": "Linear"},
        {"id": "recent:work:1", "kind": "work_pattern", "lifecycle": "blocked"},
    ]

    encoded = dreams._json_context({"signals": signals})
    payload = json.loads(encoded)
    kinds = {item["kind"] for item in payload["signals"]}

    assert len(encoded) <= dreams.MAX_CONTEXT_CHARS
    assert {"conversation", "tool_pattern", "metric_pattern", "graph_pattern", "knowledge_source", "work_pattern"} <= kinds
    assert payload["context_receipt"]["signals"]["collected"] == len(signals)


class _PlanConnection:
    def __init__(self, previous_nightly):
        self.previous_nightly = previous_nightly

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, _params=()):
        value = {"count": 4} if "count(*)" in statement else self.previous_nightly
        return type("Cursor", (), {"fetchone": lambda _self: value})()


def test_manual_plan_is_rolling_and_does_not_consume_the_nightly_cursor():
    now = datetime(2026, 8, 5, 16, tzinfo=timezone.utc)
    previous = {"window_end": now - timedelta(hours=2)}
    factory = lambda: _PlanConnection(previous)

    manual = dreams.cycle_plan(factory, now, date(2026, 8, 5), "manual")
    nightly = dreams.cycle_plan(factory, now, date(2026, 8, 5), "nightly")

    assert manual["window_start"] == now - timedelta(days=dreams.MANUAL_WINDOW_DAYS)
    assert nightly["window_start"] == previous["window_end"]
    assert manual["horizon_start"] == now - timedelta(days=dreams.HORIZON_WINDOW_DAYS)
    assert len(manual["lenses"]) == 3 and len(set(manual["lenses"])) == 3
    assert len(nightly["lenses"]) == 2


def test_private_notes_and_calendar_only_enter_as_k_anonymous_aggregates():
    worker = (_HERE / "calliope_dreams.py").read_text(encoding="utf-8")

    assert worker.count("HAVING count(DISTINCT lower(") >= 2
    collector = worker[worker.index("def collect_snapshot"):worker.index("def _json_context")]
    assert "n.body" not in collector
    assert "e.summary" not in collector
    assert '"privacy": "k-anonymous aggregate"' in collector


def test_semantic_similarity_can_deepen_an_idea_without_matching_unrelated_work():
    assert dreams.similarity(
        "Reusable weekly revenue health dashboard",
        "A weekly dashboard for reusable revenue health signals",
    ) >= 0.68
    assert dreams.similarity(
        "Reusable weekly revenue health dashboard",
        "Automate onboarding document access reviews",
    ) < 0.2


def test_model_prior_match_and_canonical_terms_converge_paraphrases():
    prior = [{
        "id": "4f19b3d4-75ef-4fb5-8f80-1b20d05d34ba",
        "fingerprint": "legacy",
        "problem_key": "revenue::weekly reporting repetition::reusable health view",
        "semantic_key": dreams.semantic_key(
            "revenue::weekly reporting repetition::reusable health view",
            ["Revenue reporting"],
        ),
        "title": "Reuse the weekly revenue health report",
        "thesis": "Teams repeatedly rebuild a weekly revenue health dashboard.",
        "entities": ["Revenue reporting"],
        "recurrence_count": 4,
    }]
    candidate = {
        "fingerprint": "new-wording",
        "problem_key": "weekly revenue reports need one reusable health dashboard",
        "semantic_key": dreams.semantic_key(
            "weekly revenue reports need one reusable health dashboard",
            ["Revenue reports"],
        ),
        "matched_prior_id": prior[0]["id"],
        "title": "Stop recreating revenue views",
        "thesis": "A governed weekly view could replace repeated dashboard assembly.",
        "entities": ["Revenue reports"],
    }

    matched, score = dreams.match_candidate(candidate, prior)

    assert matched["id"] == prior[0]["id"]
    assert score == 1.0
    assert "report" in candidate["semantic_key"]
    assert "dashboards" not in candidate["semantic_key"]


def test_bounded_portfolio_promotes_three_keeps_thirty_and_retires_tail():
    now = datetime(2026, 8, 7, 12, tzinfo=timezone.utc)
    rows = [{
        "id": str(index),
        "status": "proposed",
        "rank_score": 1 - (index / 100),
        "recurrence_count": 2,
        "created_at": now - timedelta(days=index % 4),
        "last_seen_at": now - timedelta(days=index % 4),
    } for index in range(45)]

    plan = dreams.portfolio_plan(rows, scope_kind="company", now=now)

    assert len([item for item in plan if item["portfolio_state"] == "promoted"]) == 3
    assert len([
        item for item in plan
        if item["status"] == "proposed" and item["portfolio_state"] == "backlog"
    ]) == 30
    assert len([item for item in plan if item["retired_reason"] == "portfolio_cap"]) == 12


def test_personal_portfolio_hides_feedback_and_expires_stale_one_offs():
    now = datetime(2026, 8, 7, 12, tzinfo=timezone.utc)
    rows = [
        {
            "id": "dismissed", "status": "proposed", "rank_score": 1,
            "recurrence_count": 5, "last_seen_at": now,
            "viewer_event_kind": "dismissed",
        },
        {
            "id": "stale", "status": "proposed", "rank_score": .9,
            "recurrence_count": 1, "last_seen_at": now - timedelta(days=22),
        },
        {
            "id": "fresh", "status": "proposed", "rank_score": .8,
            "recurrence_count": 1, "last_seen_at": now,
        },
    ]

    plan = {item["id"]: item for item in dreams.portfolio_plan(rows, scope_kind="personal", now=now)}

    assert plan["dismissed"]["portfolio_state"] == "backlog"
    assert plan["dismissed"]["portfolio_rank"] is None
    assert plan["stale"]["status"] == "retired"
    assert plan["stale"]["retired_reason"] == "stale_one_off"
    assert plan["fresh"]["portfolio_state"] == "promoted"


def test_personal_candidates_reject_system_meta_even_when_the_model_returns_it():
    observations = [{"id": "observation:1", "kind": "gap"}]
    result = dreams.normalize_candidates({"dreams": [
        {
            "title": "Repair the global metric scheduler",
            "thesis": "A system-wide metric failure needs an administrator to repair its ingestion path.",
            "problem_key": "metric system failure",
            "relevance_kind": "system_meta",
            "observation_ids": ["observation:1"],
        },
        {
            "title": "Finish the renewal analysis",
            "thesis": "The user's active renewal analysis has a concrete follow-up worth completing.",
            "problem_key": "renewal analysis follow up",
            "relevance_kind": "follow_up",
            "observation_ids": ["observation:1"],
        },
    ]}, observations, scope_kind="personal")

    assert [item["title"] for item in result] == ["Finish the renewal analysis"]


def test_personal_candidates_use_a_smaller_bounded_reservoir():
    observations = [{"id": "observation:1", "kind": "connection"}]
    raw = {"dreams": [
        {
            "title": f"Personal possibility {index}",
            "thesis": f"A grounded personal possibility number {index} with a concrete next step.",
            "problem_key": f"personal possibility {index}",
            "relevance_kind": "active_work",
            "observation_ids": ["observation:1"],
        }
        for index in range(12)
    ]}

    result = dreams.normalize_candidates(raw, observations, scope_kind="personal")

    assert len(result) == dreams.MAX_PERSONAL_CANDIDATES == 8


def test_dossier_observations_preserve_only_receipted_private_evidence():
    result = dreams.dossier_observations({
        "context": {
            "active_threads": [{
                "label": "Renewal exception analysis",
                "detail": "Compare renewal exceptions before the weekly operating review.",
                "evidence_ids": ["personal:request:1", "invented:1"],
                "confidence": .8,
            }],
        },
        "evidence_receipts": [{
            "id": "personal:request:1", "private": True,
            "kind": "request", "label": "Recent request",
        }],
    })

    assert len(result) == 1
    assert result[0]["scopes"] == ["personal"]
    assert result[0]["evidence_ids"] == ["personal:request:1"]
    assert result[0]["evidence"][0]["private"] is True


def test_personal_input_hash_ignores_its_own_output_but_wakes_on_user_feedback():
    dossier = {"version": 3, "input_hash": "grounded-context"}
    empty = dreams.personal_dream_input_hash(dossier, [])
    proposed = dreams.personal_dream_input_hash(dossier, [{
        "id": "dream-1", "status": "proposed", "viewer_state": "viewed",
        "version": 9, "updated_at": "2026-08-07T12:00:00Z",
    }])
    adopted = dreams.personal_dream_input_hash(dossier, [{
        "id": "dream-1", "status": "adopted", "viewer_state": "adopted",
        "version": 9, "updated_at": "2026-08-07T12:00:00Z",
    }])

    assert proposed == empty
    assert adopted != empty


def test_working_context_is_bounded_evidence_linked_and_identity_free():
    evidence = {
        "personal:session:1": {
            "id": "personal:session:1", "private": True,
            "kind": "conversation", "label": "Renewal analysis",
            "detail": "Compare the current renewal cohorts.",
            "seen_at": "2026-08-07T12:00:00+00:00",
        }
    }
    context, receipts = dreams.normalize_dossier({
        "summary": "Ada@example.com is reviewing renewal cohorts.",
        "active_threads": [{
            "label": "Renewal cohort analysis",
            "detail": "Compare current renewal cohorts and preserve the reusable view.",
            "evidence_ids": ["personal:session:1", "invented:1"],
            "confidence": 9,
        }],
        "preferences": [{
            "label": "Unsupported preference",
            "detail": "This should be dropped because it has no real evidence.",
            "evidence_ids": ["invented:2"],
        }],
    }, evidence)

    assert "ada@example.com" not in context["summary"].lower()
    assert context["active_threads"][0]["evidence_ids"] == ["personal:session:1"]
    assert context["active_threads"][0]["confidence"] == 1.0
    assert context["preferences"] == []
    assert receipts == [evidence["personal:session:1"]]


def test_personal_scope_requires_a_verified_email_and_company_scope_has_no_owner():
    assert dreams.normalize_scope("personal", "ADA@EXAMPLE.COM") == ("personal", "ada@example.com")
    assert dreams.normalize_scope("company", "ignored@example.com") == ("company", None)
    with pytest.raises(ValueError, match="verified owner"):
        dreams.normalize_scope("personal", "static-key")


def test_dream_feature_is_configurable_but_automatic_with_calliope(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "warehouse_calliope_dream_config_test_module", _HERE / "calliope.py"
    )
    calliope = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = calliope
    spec.loader.exec_module(calliope)

    monkeypatch.setenv("WAREHOUSE_HERMES_URL", "http://hermes:8642")
    monkeypatch.setenv("WAREHOUSE_HERMES_API_KEY", "hermes-key")
    monkeypatch.setenv("WAREHOUSE_CALLIOPE_DREAM_TIMEZONE", "America/New_York")
    monkeypatch.setenv("WAREHOUSE_CALLIOPE_DREAM_HOUR", "4")
    monkeypatch.setenv("WAREHOUSE_CALLIOPE_DREAM_QUEUE_POLL_SECONDS", "7")
    config = calliope.CalliopeConfig.from_env()
    assert config.dreaming_enabled is True
    assert config.dream_evidence_lab_enabled is True
    assert config.dream_timezone == "America/New_York"
    assert config.dream_hour == 4
    assert config.dream_interval_seconds == 7

    monkeypatch.setenv("WAREHOUSE_CALLIOPE_DREAMS", "0")
    assert calliope.CalliopeConfig.from_env().dreaming_enabled is False
    monkeypatch.setenv("WAREHOUSE_CALLIOPE_DREAM_EVIDENCE_LAB", "0")
    assert calliope.CalliopeConfig.from_env().dream_evidence_lab_enabled is False


def test_dream_routes_default_private_and_gate_company_to_admins(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "warehouse_calliope_dream_routes_test_module", _HERE / "calliope.py"
    )
    calliope = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = calliope
    spec.loader.exec_module(calliope)
    routes = {}

    class MCP:
        @staticmethod
        def custom_route(path, methods):
            def register(handler):
                routes[(path, tuple(methods))] = handler
                return handler
            return register

    fake_auth = types.SimpleNamespace(
        read_session_full=lambda request: request.session,
        google_enabled=lambda: False,
        background_layer=lambda *_args, **_kwargs: "",
    )
    monkeypatch.setitem(sys.modules, "auth", fake_auth)
    monkeypatch.setenv("WAREHOUSE_HERMES_URL", "http://hermes:8642")
    monkeypatch.setenv("WAREHOUSE_HERMES_API_KEY", "hermes-key")
    monkeypatch.setenv("WAREHOUSE_CALLIOPE_DIR", str(tmp_path))
    monkeypatch.setattr(calliope, "ensure_tables", lambda _factory: None)
    monkeypatch.setattr(calliope, "_start_session_synopsis_worker", lambda _factory: None)
    monkeypatch.setattr(
        calliope.calliope_dreams,
        "is_company_admin",
        lambda _factory, owner: owner == "admin@example.com",
    )
    observed = []

    def fake_snapshot(_factory, owner, *, view, limit, scope_kind):
        if scope_kind == "company" and owner != "admin@example.com":
            raise PermissionError("Company Dreams are available to the Admins Team")
        observed.append((owner, view, limit, scope_kind))
        return {"dreams": [], "counts": {}, "latest_cycle": None, "scope_kind": scope_kind}

    monkeypatch.setattr(calliope.calliope_dreams, "snapshot", fake_snapshot)
    monkeypatch.setattr(calliope.calliope_dreams, "load_user_dossier", lambda _f, _o: {})
    assert calliope.register_calliope_routes(MCP(), lambda: None, "", lambda value: value)

    class Request:
        def __init__(self, owner, query=None, body=None):
            self.session = {"identity": owner, "mapped": True, "sub": owner}
            self.query_params = query or {}
            self._body = body or {}

        async def json(self):
            return self._body

    list_route = routes[("/api/calliope/dreams", ("GET",))]
    personal = asyncio.run(list_route(Request("user@example.com")))
    assert personal.status_code == 200
    assert observed[-1][-1] == "personal"

    forbidden = asyncio.run(list_route(Request("user@example.com", {"scope": "company"})))
    assert forbidden.status_code == 403
    assert json.loads(forbidden.body)["error"]["code"] == "DREAM_SCOPE_FORBIDDEN"

    company = asyncio.run(list_route(Request("admin@example.com", {"scope": "company"})))
    assert company.status_code == 200
    assert observed[-1][-1] == "company"

    context_route = routes[("/api/calliope/dreams/context", ("GET",))]
    context = asyncio.run(context_route(Request("user@example.com")))
    assert context.status_code == 200
    assert json.loads(context.body)["context"]["available"] is False


def test_dream_schema_routes_ui_and_governed_handoff_ship_together():
    backend = (_HERE / "calliope.py").read_text(encoding="utf-8")
    worker = (_HERE / "calliope_dreams.py").read_text(encoding="utf-8")
    server = (_HERE / "server.py").read_text(encoding="utf-8")
    dockerfile = (_HERE / "Dockerfile").read_text(encoding="utf-8")
    page = (_HERE / "calliope" / "index.html").read_text(encoding="utf-8")
    script = (_HERE / "calliope" / "calliope.js").read_text(encoding="utf-8")
    styles = (_HERE / "calliope" / "calliope.css").read_text(encoding="utf-8")
    migration = (
        _HERE.parent.parent / "crates" / "pg_rvbbit" / "sql" / "migrations"
        / "0241_calliope_dreams.sql"
    ).read_text(encoding="utf-8")
    portfolio_migration = (
        _HERE.parent.parent / "crates" / "pg_rvbbit" / "sql" / "migrations"
        / "0242_calliope_dream_portfolio.sql"
    ).read_text(encoding="utf-8")
    evidence_lab_migration = (
        _HERE.parent.parent / "crates" / "pg_rvbbit" / "sql" / "migrations"
        / "0243_calliope_dream_evidence_lab.sql"
    ).read_text(encoding="utf-8")
    personal_migration = (
        _HERE.parent.parent / "crates" / "pg_rvbbit" / "sql" / "migrations"
        / "0255_calliope_personal_dreams.sql"
    ).read_text(encoding="utf-8")
    scheduler_migration = (
        _HERE.parent.parent / "crates" / "pg_rvbbit" / "sql" / "migrations"
        / "0256_calliope_dream_scheduler.sql"
    ).read_text(encoding="utf-8")
    scheduler_seed = (
        _HERE.parent.parent / "docker" / "init" / "03-seed-scheduled-tasks.sh"
    ).read_text(encoding="utf-8")
    registry = (
        _HERE.parent.parent / "crates" / "pg_rvbbit" / "src" / "migrations.rs"
    ).read_text(encoding="utf-8")

    assert backend.count('"/api/calliope/dreams') >= 4
    assert "turn_kind)" in backend and "'dream'" in backend
    assert '"kind": "dream"' in backend
    assert "start_dream_worker" in server
    assert "calliope_dreams.py" in dockerfile
    assert "OBSERVER_INSTRUCTIONS" in worker and "IDEATOR_INSTRUCTIONS" in worker
    assert "INVESTIGATOR_INSTRUCTIONS" in worker and "ASSESSOR_INSTRUCTIONS" in worker
    assert "SET TRANSACTION READ ONLY" in worker and "SAFE_CLOVER_AFFORDANCES" in worker
    assert "raw chat exists only for a model call" in worker
    assert "observe_recent" in worker and "observe_horizon" in worker
    assert "MAX_CANDIDATES = 12" in worker and "MAX_DREAMS = 3" in worker
    assert "MAX_PERSONAL_CANDIDATES = 8" in worker
    assert "PERSONAL_IDEATOR_INSTRUCTIONS" in worker and "DOSSIER_INSTRUCTIONS" in worker
    assert "active_personal_users" in worker and "scope_kind=\"personal\"" in worker
    assert 'id="calliope-dreams-dialog"' in page
    assert 'data-dream-view="backlog"' in page and "Dream deeper" in page
    assert 'data-dream-scope="personal"' in page and 'data-dream-scope="company"' in page
    assert 'id="calliope-dreams-context"' in page
    assert "function renderDreamSurface" in script
    assert "function markDreamViewed" in script
    assert "function loadDreamContext" in script and "function setDreamScope" in script
    assert "scope=${encodeURIComponent(scope)}" in script
    assert "function dreamProbeMarkup" in script and "What Calliope tested" in script
    assert ".calliope-dreams-dialog" in styles and ".dream-surface" in styles
    assert ".calliope-dream-tests" in styles
    assert ".calliope-dreams-context" in styles and ".calliope-dreams-scope" in styles
    assert '"/api/calliope/dreams/context"' in backend
    assert "DREAM_SCOPE_FORBIDDEN" in backend and "can_view_company" in backend
    assert "CREATE TABLE IF NOT EXISTS rvbbit.calliope_dreams" in migration
    assert "user_message" not in migration
    assert '"0241_calliope_dreams"' in registry
    assert "portfolio_state" in portfolio_migration and "candidate_count" in portfolio_migration
    assert '"0242_calliope_dream_portfolio"' in registry
    assert "CREATE TABLE IF NOT EXISTS rvbbit.calliope_dream_probes" in evidence_lab_migration
    assert "probe_receipts" in evidence_lab_migration
    assert '"0243_calliope_dream_evidence_lab"' in registry
    assert "scope_kind" in personal_migration and "owner_email" in personal_migration
    assert "CREATE TABLE IF NOT EXISTS rvbbit.calliope_user_dossiers" in personal_migration
    assert "calliope_dreams_scope_fingerprint_key" in personal_migration
    assert '"0255_calliope_personal_dreams"' in registry
    assert "CREATE TABLE IF NOT EXISTS rvbbit.calliope_dream_sweeps" in scheduler_migration
    assert "CREATE TABLE IF NOT EXISTS rvbbit.calliope_dream_jobs" in scheduler_migration
    assert "CREATE OR REPLACE FUNCTION rvbbit.calliope_dream_enqueue" in scheduler_migration
    assert "never private dossier or Dream content" in scheduler_migration
    assert '"0256_calliope_dream_scheduler"' in registry
    worker_loop = worker[worker.index("def _worker("):worker.index("def start_worker(")]
    assert "_dream_queue_tick" in worker_loop
    assert "cycle_due" not in worker_loop
    assert "WAREHOUSE_CALLIOPE_DREAM_QUEUE_POLL_SECONDS" in backend
    assert "rvbbit_calliope_dreams" in scheduler_seed
    assert "'0 3 * * *'" in scheduler_seed
    assert "SET active=false" in scheduler_seed
