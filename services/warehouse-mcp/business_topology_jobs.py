"""Durable Business Topology excavation worker for the Warehouse appliance.

PostgreSQL is the control plane: DataRabbit inserts one bounded workflow row,
this standing worker leases it, and every visible phase/progress update is
written back to SQL.  Private intermediate artifacts live in Warehouse's
durable data volume and are resumable after a process/container restart.

The worker stages validated proposal bundles only.  It never promotes or
materializes governed Business Topology.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import threading
import time
import traceback
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row


# Local source checkouts keep the reusable evaluator under bench/.  The image
# installs that same package directly at /app/business_topology.
try:
    from business_topology.bundles import stage_execution_bundles
except ModuleNotFoundError:
    _repo_bench = Path(__file__).resolve().parents[2] / "bench"
    if _repo_bench.is_dir():
        sys.path.insert(0, str(_repo_bench))
    from business_topology.bundles import stage_execution_bundles

from business_topology.candidates import pair_key
from business_topology.domains import propose_source_neighborhoods
from business_topology.embedding import (
    build_embedding_evidence,
    embed_postgres_shadow,
    make_embedding_inputs,
)
from business_topology.excavation import build_excavation_plan
from business_topology.extract import (
    extract_postgres_overlap_shadow,
    extract_postgres_shadow_corpus,
)
from business_topology.worker import (
    HutchChatClient,
    WORKER_VERSION as EXCAVATION_WORKER_VERSION,
    execute_plan,
    execution_preview,
    select_work,
)


WORKFLOW_WORKER_VERSION = "business-topology-appliance-worker-v1"
_THREAD: threading.Thread | None = None
_THREAD_LOCK = threading.Lock()
_STOP = threading.Event()
_MIGRATION_MISSING_LOGGED = False


class WorkflowLeaseLost(RuntimeError):
    """The run was cancelled or is no longer owned by this worker."""


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return min(max(value, minimum), maximum)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _private_root() -> Path:
    path = Path(
        os.environ.get(
            "WAREHOUSE_BUSINESS_TOPOLOGY_DIR",
            "/app/data/business-topology",
        )
    )
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def _write_private_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    os.chmod(path, 0o600)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def _load_or_create(path: Path, create: Any) -> Any:
    if path.is_file():
        return _read_json(path)
    value = create()
    _write_private_json(path, value)
    return value


def _connect(dsn: str, *, autocommit: bool = False, dict_rows: bool = False):
    options: dict[str, Any] = {"autocommit": autocommit}
    if dict_rows:
        options["row_factory"] = dict_row
    return psycopg.connect(dsn, **options)


def _worker_id() -> str:
    configured = os.environ.get("WAREHOUSE_BUSINESS_TOPOLOGY_WORKER_ID", "").strip()
    if configured:
        return configured[:240]
    return f"warehouse-mcp:{socket.gethostname()}:{os.getpid()}"


def _register_worker(
    dsn: str,
    worker_id: str,
    status: str,
    details: Mapping[str, Any] | None = None,
) -> None:
    with _connect(dsn, autocommit=True) as conn:
        conn.execute(
            "SELECT rvbbit.business_topology_register_workflow_worker(%s,%s,%s,%s::jsonb)",
            (
                worker_id,
                WORKFLOW_WORKER_VERSION,
                status,
                json.dumps(dict(details or {})),
            ),
        )


def _claim(dsn: str, worker_id: str, lease_seconds: int) -> dict[str, Any] | None:
    with _connect(dsn, autocommit=True, dict_rows=True) as conn:
        row = conn.execute(
            "SELECT * FROM rvbbit.business_topology_claim_workflow(%s,%s)",
            (worker_id, lease_seconds),
        ).fetchone()
    return dict(row) if row else None


def _update(
    dsn: str,
    worker_id: str,
    run_id: str,
    phase: str,
    progress: Mapping[str, Any] | None = None,
    *,
    lease_seconds: int,
) -> None:
    with _connect(dsn, autocommit=True, dict_rows=True) as conn:
        row = conn.execute(
            "SELECT rvbbit.business_topology_update_workflow(%s::uuid,%s,%s,%s::jsonb,%s)",
            (run_id, worker_id, phase, json.dumps(dict(progress or {})), lease_seconds),
        ).fetchone()
    if not row or row["business_topology_update_workflow"] is not True:
        raise WorkflowLeaseLost(f"workflow {run_id} was cancelled or its lease was lost")


def _complete(
    dsn: str,
    worker_id: str,
    run_id: str,
    result: Mapping[str, Any],
) -> None:
    with _connect(dsn, autocommit=True) as conn:
        conn.execute(
            "SELECT rvbbit.business_topology_complete_workflow(%s::uuid,%s,%s::jsonb)",
            (run_id, worker_id, json.dumps(dict(result))),
        )


def _fail(
    dsn: str,
    worker_id: str,
    run_id: str,
    error: str,
    progress: Mapping[str, Any] | None = None,
) -> None:
    with _connect(dsn, autocommit=True) as conn:
        conn.execute(
            "SELECT rvbbit.business_topology_fail_workflow(%s::uuid,%s,%s,%s::jsonb)",
            (run_id, worker_id, error[:6000], json.dumps(dict(progress or {}))),
        )


class _LeaseKeeper:
    def __init__(self, dsn: str, worker_id: str, run_id: str, lease_seconds: int):
        self.dsn = dsn
        self.worker_id = worker_id
        self.run_id = run_id
        self.lease_seconds = lease_seconds
        self.stop_event = threading.Event()
        self.lost = threading.Event()
        self.thread = threading.Thread(
            target=self._run,
            name=f"topology-lease-{run_id[:8]}",
            daemon=True,
        )

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=5)

    def assert_owned(self) -> None:
        if self.lost.is_set():
            raise WorkflowLeaseLost(
                f"workflow {self.run_id} was cancelled or its lease was lost"
            )

    def _run(self) -> None:
        interval = min(max(self.lease_seconds // 3, 10), 30)
        while not self.stop_event.wait(interval):
            try:
                with _connect(self.dsn, autocommit=True, dict_rows=True) as conn:
                    row = conn.execute(
                        "SELECT rvbbit.business_topology_renew_workflow_lease(%s::uuid,%s,%s)",
                        (self.run_id, self.worker_id, self.lease_seconds),
                    ).fetchone()
                if not row or row["business_topology_renew_workflow_lease"] is not True:
                    self.lost.set()
                    return
                _register_worker(
                    self.dsn,
                    self.worker_id,
                    "busy",
                    {"run_id": self.run_id, "excavation_worker": EXCAVATION_WORKER_VERSION},
                )
            except Exception as exc:  # noqa: BLE001 - the main lease still has time
                print(
                    f"business topology lease heartbeat failed run={self.run_id}: {exc}",
                    file=sys.stderr,
                )


def _embedding_rows(
    dsn: str,
    corpus: Mapping[str, Any],
    *,
    kinds: Sequence[str],
    channels: Sequence[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    inputs = make_embedding_inputs(corpus, kinds=kinds, channels=channels)
    with _connect(dsn) as conn:
        vectors, audit = embed_postgres_shadow(
            conn,
            inputs,
            specialist="embed",
            mode="document",
            max_batch_items=16,
            max_batch_chars=24_000,
            statement_timeout_ms=300_000,
        )
    failures = audit.get("failures") or []
    if failures:
        raise RuntimeError(
            f"embedding failed for {len(failures)} of {len(inputs)} topology packets"
        )
    return vectors, audit


def _merge_evidence(
    *groups: Iterable[Mapping[str, Any]],
) -> dict[str, dict[str, float]]:
    merged: dict[str, dict[str, float]] = {}
    for group in groups:
        for item in group:
            left = str(item.get("left_population_id") or "")
            right = str(item.get("right_population_id") or "")
            values = item.get("local_evidence")
            if not left or not right or left == right or not isinstance(values, Mapping):
                continue
            target = merged.setdefault(pair_key(left, right), {})
            for key, value in values.items():
                if isinstance(value, (int, float)):
                    target[str(key)] = max(float(value), target.get(str(key), float("-inf")))
    return merged


def _probe_pairs(
    evidence: Mapping[str, Mapping[str, float]],
    *,
    limit: int,
) -> list[tuple[str, str]]:
    def priority(key: str) -> tuple[float, float, float, str]:
        values = evidence[key]
        return (
            -float(values.get("query_join_count", 0.0)),
            -float(values.get("query_cooccurrence_count", 0.0)),
            -float(values.get("embedding_similarity", -1.0)),
            key,
        )

    result: list[tuple[str, str]] = []
    for key in sorted(evidence, key=priority)[:limit]:
        left, right = key.split("\x1f", 1)
        result.append((left, right))
    return result


class _ExecutionProgress:
    def __init__(
        self,
        dsn: str,
        worker_id: str,
        run_id: str,
        lease: _LeaseKeeper,
        lease_seconds: int,
        total: int,
    ):
        self.dsn = dsn
        self.worker_id = worker_id
        self.run_id = run_id
        self.lease = lease
        self.lease_seconds = lease_seconds
        self.total = max(total, 1)
        self.last_update = 0.0

    def __call__(self, event: Mapping[str, Any]) -> None:
        self.lease.assert_owned()
        now = time.monotonic()
        work_kind = str(event.get("work_kind") or "")
        force = (
            event.get("event") in {"started", "finished", "failed"}
            or work_kind != "correspondence"
            or now - self.last_update >= 0.75
        )
        if not force:
            return
        completed = int(event.get("completed_work_items") or 0)
        _update(
            self.dsn,
            self.worker_id,
            self.run_id,
            "executing",
            {
                "work_item_count": self.total,
                "completed_work_items": completed,
                "work_progress": round(completed / self.total, 6),
                "current_work_id": event.get("work_id"),
                "current_work_kind": work_kind or None,
                "work_event": event.get("event"),
                "resumed_work_items": int(event.get("resumed_work_items") or 0),
                "local_correspondence_calls": int(
                    event.get("local_correspondence_calls") or 0
                ),
                "llm_attempts": int(event.get("hutch_llm_attempts") or 0),
            },
            lease_seconds=self.lease_seconds,
        )
        self.last_update = now


def _run_workflow(
    dsn: str,
    worker_id: str,
    job: Mapping[str, Any],
    *,
    lease_seconds: int,
) -> dict[str, Any]:
    run_id = str(job["run_id"])
    params = dict(job.get("parameters") or {})
    relations = [str(value) for value in params.get("relations") or []]
    if not relations:
        raise RuntimeError("workflow has no relation scope")

    run_root = _private_root() / run_id
    run_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(run_root, 0o700)
    parameter_hash = _sha256(params)
    manifest_path = run_root / "workflow.json"
    if manifest_path.is_file():
        manifest = _read_json(manifest_path)
        if manifest.get("parameters_sha256") != parameter_hash:
            raise RuntimeError("private workflow directory belongs to different parameters")
    else:
        _write_private_json(
            manifest_path,
            {
                "schema_version": "rvbbit.business-topology.appliance-workflow.v1",
                "run_id": run_id,
                "parameters_sha256": parameter_hash,
                "parameters": params,
            },
        )

    lease = _LeaseKeeper(dsn, worker_id, run_id, lease_seconds)
    lease.start()
    try:
        sample_rows = int(params.get("sample_rows") or 2048)
        corpus_path = run_root / "corpus.private.json"
        _update(
            dsn,
            worker_id,
            run_id,
            "profiling",
            {"relation_count": len(relations), "current_step": "Profiling source populations"},
            lease_seconds=lease_seconds,
        )

        extraction_audit: dict[str, Any] = {}

        def create_corpus() -> dict[str, Any]:
            nonlocal extraction_audit
            with _connect(dsn) as conn:
                corpus, extraction_audit = extract_postgres_shadow_corpus(
                    conn,
                    relations,
                    corpus_id=str(params.get("corpus_id") or f"business-topology-{run_id}"),
                    sample_rows=sample_rows,
                    statement_timeout_ms=120_000,
                )
            return corpus

        corpus = _load_or_create(corpus_path, create_corpus)
        population_count = len(corpus.get("populations") or [])
        _update(
            dsn,
            worker_id,
            run_id,
            "profiling",
            {
                "population_count": population_count,
                "profiled_relations": len(relations),
                "extraction_audit": extraction_audit,
            },
            lease_seconds=lease_seconds,
        )
        lease.assert_owned()

        context_vectors_path = run_root / "context-vectors.private.json"
        _update(
            dsn,
            worker_id,
            run_id,
            "embedding_sources",
            {"current_step": "Finding source neighborhoods"},
            lease_seconds=lease_seconds,
        )
        context_audit: dict[str, Any] = {}

        def create_context_vectors() -> list[dict[str, Any]]:
            nonlocal context_audit
            vectors, context_audit = _embedding_rows(
                dsn,
                corpus,
                kinds=("record_context",),
                channels=("context",),
            )
            return vectors

        context_vectors = _load_or_create(context_vectors_path, create_context_vectors)
        neighborhoods_path = run_root / "neighborhoods.private.json"
        neighborhoods = _load_or_create(
            neighborhoods_path,
            lambda: propose_source_neighborhoods(corpus, context_vectors),
        )
        neighborhood_summary = dict(neighborhoods.get("summary") or {})
        _update(
            dsn,
            worker_id,
            run_id,
            "partitioning",
            {
                "neighborhood_count": len(neighborhoods.get("neighborhoods") or []),
                "singleton_source_count": int(
                    neighborhood_summary.get("singleton_sources") or 0
                ),
                "bridge_count": int(neighborhood_summary.get("bridges") or 0),
                "source_embedding_audit": context_audit,
            },
            lease_seconds=lease_seconds,
        )
        lease.assert_owned()

        field_vectors_path = run_root / "field-vectors.private.json"
        _update(
            dsn,
            worker_id,
            run_id,
            "embedding_populations",
            {"current_step": "Comparing semantic populations"},
            lease_seconds=lease_seconds,
        )
        field_audit: dict[str, Any] = {}

        def create_field_vectors() -> list[dict[str, Any]]:
            nonlocal field_audit
            vectors, field_audit = _embedding_rows(
                dsn,
                corpus,
                kinds=("field",),
                channels=("focus", "context"),
            )
            return vectors

        field_vectors = _load_or_create(field_vectors_path, create_field_vectors)
        semantic_path = run_root / "semantic-evidence.private.json"
        semantic_evidence = _load_or_create(
            semantic_path,
            lambda: build_embedding_evidence(corpus, field_vectors, top_k=24),
        )
        semantic_map = _merge_evidence(semantic_evidence)

        overlap_path = run_root / "overlap-evidence.private.json"
        overlap_audit: dict[str, Any] = {}

        def create_overlap() -> list[dict[str, Any]]:
            nonlocal overlap_audit
            with _connect(dsn) as conn:
                evidence, overlap_audit = extract_postgres_overlap_shadow(
                    conn,
                    corpus,
                    probe_pairs=_probe_pairs(semantic_map, limit=1000),
                    sample_rows=sample_rows,
                    min_shared=1,
                    max_fingerprint_fanout=50,
                    max_pairs=50_000,
                    max_probe_pairs=1000,
                    statement_timeout_ms=300_000,
                )
            return evidence

        _update(
            dsn,
            worker_id,
            run_id,
            "evidence",
            {
                "semantic_candidate_count": len(semantic_evidence),
                "current_step": "Testing local overlap evidence",
            },
            lease_seconds=lease_seconds,
        )
        overlap_evidence = _load_or_create(overlap_path, create_overlap)
        merged_evidence = _merge_evidence(semantic_evidence, overlap_evidence)
        _update(
            dsn,
            worker_id,
            run_id,
            "evidence",
            {
                "semantic_candidate_count": len(semantic_evidence),
                "overlap_candidate_count": len(overlap_evidence),
                "candidate_pair_count": len(merged_evidence),
                "population_embedding_audit": field_audit,
                "overlap_audit": overlap_audit,
            },
            lease_seconds=lease_seconds,
        )
        lease.assert_owned()

        policy = dict(params.get("policy") or {})
        plan_path = run_root / "excavation-plan.private.json"
        _update(
            dsn,
            worker_id,
            run_id,
            "planning",
            {"current_step": "Compiling the bounded excavation DAG"},
            lease_seconds=lease_seconds,
        )
        plan = _load_or_create(
            plan_path,
            lambda: build_excavation_plan(
                corpus,
                neighborhoods,
                evidence_by_pair=merged_evidence,
                maximum_sources_per_unit=int(
                    policy.get("maximum_sources_per_unit") or 12
                ),
                maximum_populations_per_unit=int(
                    policy.get("maximum_populations_per_unit") or 48
                ),
                max_pairs_per_unit=int(policy.get("max_pairs_per_unit") or 64),
                max_pairs_per_link=int(policy.get("max_pairs_per_link") or 8),
                max_population_fanout=int(
                    policy.get("max_population_fanout") or 8
                ),
                minimum_pair_priority=float(
                    policy.get("minimum_pair_priority") or 0.32
                ),
                max_cross_neighborhood_links=int(
                    policy.get("max_cross_neighborhood_links") or 24
                ),
            ),
        )
        selected = select_work(plan, all_work=True)
        preview = execution_preview(plan, selected)
        plan_summary = dict(plan.get("summary") or {})
        plan_sha256 = _sha256(plan)
        _update(
            dsn,
            worker_id,
            run_id,
            "planning",
            {
                "plan_sha256": plan_sha256,
                "excavation_unit_count": int(
                    plan_summary.get("excavation_units") or 0
                ),
                "boundary_link_count": int(plan_summary.get("boundary_links") or 0),
                "work_item_count": len(selected),
                "completed_work_items": 0,
                "generative_calls_planned": int(
                    preview.get("generative_calls_before_repairs") or 0
                ),
                "plan_summary": plan_summary,
            },
            lease_seconds=lease_seconds,
        )
        lease.assert_owned()

        max_work_items = int(params.get("max_work_items") or 500)
        max_llm_calls = int(params.get("max_llm_calls") or 128)
        client = HutchChatClient.from_postgres(
            dsn,
            backend_name=str(params.get("backend") or "clover_llm"),
            model=str(params.get("model") or "clover"),
            request_user=str(job.get("requested_by") or "business-topology-worker"),
            timeout_seconds=180.0,
            transport_attempts=3,
        )
        execution_dir = run_root / "execution.private"
        execution_summary = execute_plan(
            plan,
            selected,
            output_dir=execution_dir,
            client=client,
            max_work_items=max_work_items,
            max_llm_calls=max_llm_calls,
            repair_attempts=1,
            progress_callback=_ExecutionProgress(
                dsn,
                worker_id,
                run_id,
                lease,
                lease_seconds,
                len(selected),
            ),
        )
        lease.assert_owned()

        _update(
            dsn,
            worker_id,
            run_id,
            "staging",
            {"current_step": "Staging validated proposal bundles"},
            lease_seconds=lease_seconds,
        )
        with _connect(dsn) as conn:
            staging = stage_execution_bundles(
                conn,
                plan,
                execution_dir,
                include_bridges=bool(params.get("include_bridges", False)),
                proposed_by=str(job.get("requested_by") or "business-topology-worker"),
            )
        bundle_ids = [str(value) for value in staging.get("bundle_ids") or []]
        if bundle_ids:
            with _connect(dsn) as conn:
                conn.execute(
                    """
                    UPDATE rvbbit.business_topology_proposal_bundles
                       SET workflow_run_id=%s::uuid
                     WHERE bundle_id=ANY(%s::uuid[])
                    """,
                    (run_id, bundle_ids),
                )
                conn.commit()

        result = {
            "schema_version": "rvbbit.business-topology.workflow-result.v1",
            "plan_sha256": plan_sha256,
            "relation_count": len(relations),
            "population_count": population_count,
            "neighborhood_count": len(neighborhoods.get("neighborhoods") or []),
            "excavation_unit_count": int(plan_summary.get("excavation_units") or 0),
            "work_item_count": len(selected),
            "completed_work_items": len(selected),
            "llm_attempts": int(execution_summary.get("hutch_llm_attempts") or 0),
            "local_correspondence_calls": int(
                execution_summary.get("local_correspondence_calls") or 0
            ),
            "bundles_staged": int(staging.get("staged") or 0),
            "bundle_ids": bundle_ids,
            "materialized_topology": False,
            "submitted_proposals": True,
        }
        return result
    finally:
        lease.stop()


def _worker_loop(dsn: str) -> None:
    global _MIGRATION_MISSING_LOGGED
    worker_id = _worker_id()
    poll_seconds = _env_int(
        "WAREHOUSE_BUSINESS_TOPOLOGY_POLL_SECONDS", 2, 1, 60
    )
    lease_seconds = _env_int(
        "WAREHOUSE_BUSINESS_TOPOLOGY_LEASE_SECONDS", 300, 30, 3600
    )
    while not _STOP.is_set():
        try:
            _register_worker(
                dsn,
                worker_id,
                "ready",
                {"excavation_worker": EXCAVATION_WORKER_VERSION},
            )
            _MIGRATION_MISSING_LOGGED = False
            job = _claim(dsn, worker_id, lease_seconds)
            if job is None:
                _STOP.wait(poll_seconds)
                continue
            run_id = str(job["run_id"])
            print(
                f"business topology workflow claimed run={run_id} worker={worker_id}",
                file=sys.stderr,
            )
            _register_worker(
                dsn,
                worker_id,
                "busy",
                {"run_id": run_id, "excavation_worker": EXCAVATION_WORKER_VERSION},
            )
            try:
                result = _run_workflow(
                    dsn,
                    worker_id,
                    job,
                    lease_seconds=lease_seconds,
                )
                _complete(dsn, worker_id, run_id, result)
                print(
                    f"business topology workflow completed run={run_id} "
                    f"bundles={result.get('bundles_staged', 0)}",
                    file=sys.stderr,
                )
            except Exception as exc:  # noqa: BLE001 - durable failure boundary
                error = f"{type(exc).__name__}: {exc}"
                print(
                    f"business topology workflow failed run={run_id}: {error}\n"
                    f"{traceback.format_exc(limit=12)}",
                    file=sys.stderr,
                )
                try:
                    _fail(
                        dsn,
                        worker_id,
                        run_id,
                        error,
                        {"error_class": type(exc).__name__},
                    )
                except Exception as mark_exc:  # noqa: BLE001
                    print(
                        f"business topology workflow could not record failure "
                        f"run={run_id}: {mark_exc}",
                        file=sys.stderr,
                    )
        except psycopg.Error as exc:
            # Old databases simply do not have migration 0285 yet.  Stay quiet
            # after the first diagnostic and begin working automatically once
            # pg_rvbbit is upgraded/migrated.
            if not _MIGRATION_MISSING_LOGGED:
                print(
                    f"business topology workflow worker waiting for SQL contract: {exc}",
                    file=sys.stderr,
                )
                _MIGRATION_MISSING_LOGGED = True
            _STOP.wait(max(poll_seconds, 15))
        except Exception as exc:  # noqa: BLE001 - keep the standing worker alive
            print(
                f"business topology workflow worker loop error: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            _STOP.wait(max(poll_seconds, 5))


def start_worker(dsn: str) -> bool:
    """Start the singleton standing worker; return whether it is enabled."""

    global _THREAD
    if not _env_bool("WAREHOUSE_BUSINESS_TOPOLOGY_WORKER", True):
        return False
    with _THREAD_LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return True
        _STOP.clear()
        _THREAD = threading.Thread(
            target=_worker_loop,
            args=(dsn,),
            name="business-topology-workflows",
            daemon=True,
        )
        _THREAD.start()
    return True


def stop_worker() -> None:
    _STOP.set()
