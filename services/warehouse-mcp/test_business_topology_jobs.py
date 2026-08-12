from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "bench"))
sys.path.insert(0, str(REPO_ROOT / "services" / "warehouse-mcp"))

import business_topology_jobs as jobs  # noqa: E402


class _FakeConnection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...] | None]] = []
        self.commits = 0

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: str, parameters: tuple[Any, ...] | None = None):
        self.executed.append((statement, parameters))
        return self

    def commit(self) -> None:
        self.commits += 1


class _FakeLease:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def assert_owned(self) -> None:
        assert self.started and not self.stopped


def test_full_workflow_orchestration_persists_private_resume_points(monkeypatch, tmp_path):
    run_id = str(uuid.uuid4())
    bundle_id = str(uuid.uuid4())
    connection = _FakeConnection()
    phase_updates: list[tuple[str, dict[str, Any]]] = []
    creation_calls = {
        "extract": 0,
        "embed": 0,
        "neighborhoods": 0,
        "semantic": 0,
        "overlap": 0,
        "plan": 0,
    }

    corpus = {
        "schema_version": "rvbbit.business-topology.eval-corpus.v1",
        "corpus_id": f"business-topology-{run_id}",
        "populations": [
            {"population_id": "source:context"},
            {"population_id": "source:id"},
            {"population_id": "source:name"},
        ],
    }
    neighborhoods = {
        "neighborhoods": [{"neighborhood_id": "area-1"}],
        "summary": {"singleton_sources": 1, "bridges": 0},
    }
    semantic = [
        {
            "left_population_id": "source:id",
            "right_population_id": "source:name",
            "local_evidence": {"embedding_similarity": 0.72},
        }
    ]
    overlap = [
        {
            "left_population_id": "source:id",
            "right_population_id": "source:name",
            "local_evidence": {"shared_fingerprint_count": 3},
        }
    ]
    plan = {
        "schema_version": "rvbbit.business-topology.excavation-plan.v1",
        "summary": {"excavation_units": 1, "boundary_links": 0},
        "work_items": [{"work_id": "motif-1"}, {"work_id": "synthesis-1"}],
    }

    def extract(*_args: object, **_kwargs: object):
        creation_calls["extract"] += 1
        return corpus, {"rolled_back": True}

    def embed(*_args: object, **_kwargs: object):
        creation_calls["embed"] += 1
        return [{"population_id": "source:context", "vector": [1.0, 0.0]}], {
            "failures": []
        }

    def find_neighborhoods(*_args: object, **_kwargs: object):
        creation_calls["neighborhoods"] += 1
        return neighborhoods

    def build_semantic(*_args: object, **_kwargs: object):
        creation_calls["semantic"] += 1
        return semantic

    def extract_overlap(*_args: object, **_kwargs: object):
        creation_calls["overlap"] += 1
        return overlap, {"rolled_back": True}

    def build_plan(*_args: object, **_kwargs: object):
        creation_calls["plan"] += 1
        return plan

    def execute(
        _plan: dict[str, Any],
        selected: list[str],
        *,
        progress_callback: Any,
        **_kwargs: object,
    ) -> dict[str, Any]:
        progress_callback(
            {
                "event": "started",
                "total_work_items": len(selected),
                "completed_work_items": 0,
            }
        )
        progress_callback(
            {
                "event": "completed",
                "work_id": selected[-1],
                "work_kind": "neighborhood_synthesis",
                "completed_work_items": len(selected),
                "hutch_llm_attempts": 2,
            }
        )
        progress_callback(
            {
                "event": "finished",
                "completed_work_items": len(selected),
                "hutch_llm_attempts": 2,
            }
        )
        return {"hutch_llm_attempts": 2, "local_correspondence_calls": 0}

    monkeypatch.setattr(jobs, "_private_root", lambda: tmp_path)
    monkeypatch.setattr(jobs, "_connect", lambda *_args, **_kwargs: connection)
    monkeypatch.setattr(jobs, "_LeaseKeeper", _FakeLease)
    monkeypatch.setattr(
        jobs,
        "_update",
        lambda _dsn, _worker, _run, phase, progress=None, **_kwargs: phase_updates.append(
            (phase, dict(progress or {}))
        ),
    )
    monkeypatch.setattr(jobs, "extract_postgres_shadow_corpus", extract)
    monkeypatch.setattr(jobs, "_embedding_rows", embed)
    monkeypatch.setattr(jobs, "propose_source_neighborhoods", find_neighborhoods)
    monkeypatch.setattr(jobs, "build_embedding_evidence", build_semantic)
    monkeypatch.setattr(jobs, "extract_postgres_overlap_shadow", extract_overlap)
    monkeypatch.setattr(jobs, "build_excavation_plan", build_plan)
    monkeypatch.setattr(jobs, "select_work", lambda *_args, **_kwargs: ["motif-1", "synthesis-1"])
    monkeypatch.setattr(
        jobs,
        "execution_preview",
        lambda *_args, **_kwargs: {"generative_calls_before_repairs": 2},
    )
    monkeypatch.setattr(jobs, "execute_plan", execute)
    monkeypatch.setattr(
        jobs,
        "stage_execution_bundles",
        lambda *_args, **_kwargs: {"staged": 1, "bundle_ids": [bundle_id]},
    )
    monkeypatch.setattr(
        jobs.HutchChatClient,
        "from_postgres",
        classmethod(lambda _cls, *_args, **_kwargs: object()),
    )

    job = {
        "run_id": run_id,
        "requested_by": "archaeologist@example.com",
        "parameters": {
            "schema_version": "rvbbit.business-topology.workflow-parameters.v1",
            "corpus_id": f"business-topology-{run_id}",
            "relations": ["public.sample_business_data"],
            "sample_rows": 64,
            "max_work_items": 10,
            "max_llm_calls": 4,
            "backend": "clover_llm",
            "model": "clover",
            "policy": {},
        },
    }

    first = jobs._run_workflow("postgresql://unused", "worker-1", job, lease_seconds=300)
    assert first["bundles_staged"] == 1
    assert first["bundle_ids"] == [bundle_id]
    assert first["materialized_topology"] is False
    assert first["submitted_proposals"] is True
    assert first["completed_work_items"] == 2
    assert "executing" in [phase for phase, _progress in phase_updates]
    assert [phase for phase, _progress in phase_updates][-1] == "staging"
    assert creation_calls == {
        "extract": 1,
        "embed": 2,
        "neighborhoods": 1,
        "semantic": 1,
        "overlap": 1,
        "plan": 1,
    }

    run_root = tmp_path / run_id
    expected_artifacts = {
        "workflow.json",
        "corpus.private.json",
        "context-vectors.private.json",
        "neighborhoods.private.json",
        "field-vectors.private.json",
        "semantic-evidence.private.json",
        "overlap-evidence.private.json",
        "excavation-plan.private.json",
    }
    assert expected_artifacts <= {path.name for path in run_root.iterdir()}
    assert all(
        oct((run_root / name).stat().st_mode & 0o777) == "0o600"
        for name in expected_artifacts
    )
    assert oct(run_root.stat().st_mode & 0o777) == "0o700"

    # A reclaimed run reuses every expensive private checkpoint. The execution
    # layer is independently resumable and decides which work receipts remain.
    second = jobs._run_workflow("postgresql://unused", "worker-2", job, lease_seconds=300)
    assert second["bundles_staged"] == 1
    assert creation_calls == {
        "extract": 1,
        "embed": 2,
        "neighborhoods": 1,
        "semantic": 1,
        "overlap": 1,
        "plan": 1,
    }
    assert connection.commits == 2
    assert any("workflow_run_id" in statement for statement, _params in connection.executed)

    # The test never relies on permissive process umasks for private artifacts.
    assert os.access(run_root / "workflow.json", os.R_OK)
