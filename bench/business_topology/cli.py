"""Command-line interface for topology extraction, review, and evaluation."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .candidates import build_candidate_queue
from .bundles import stage_execution_bundles
from .contracts import load_corpus, write_corpus
from .extract import (
    connect_from_env,
    corpus_from_relation_profiles,
    extract_postgres_overlap_shadow,
    extract_postgres_shadow_corpus,
)
from .embedding import (
    build_embedding_evidence,
    embed_postgres_shadow,
    make_embedding_inputs,
)
from .excavation import build_excavation_plan
from .domains import (
    evaluate_source_neighborhood_controls,
    propose_source_neighborhoods,
)
from .labels import apply_label_overlay, make_label_template
from .metrics import evaluate_candidate_recall, evaluate_corpus
from .results import validate_excavation_result
from .synthetic import make_synthetic_corpus
from .train import train_corpus_baseline
from .worker import (
    HutchChatClient,
    WORK_RECEIPT_SCHEMA_VERSION,
    execute_plan,
    execution_preview,
    select_work,
)


def _write_json(path: str | None, value: Any) -> None:
    rendered = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if path in (None, "-"):
        sys.stdout.write(rendered)
    else:
        Path(path).write_text(rendered)


def _write_jsonl(path: str | None, values: list[dict[str, Any]]) -> None:
    rendered = "".join(json.dumps(value, sort_keys=True) + "\n" for value in values)
    if path in (None, "-"):
        sys.stdout.write(rendered)
    else:
        Path(path).write_text(rendered)


def _load_scope(path: str) -> list[str | dict[str, Any]]:
    value = json.loads(Path(path).read_text())
    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        raise SystemExit("scope must be an array or an object")
    if value.get("schema_version") not in (None, "rvbbit.business-topology.scope.v1"):
        raise SystemExit("unsupported scope schema_version")
    relations = value.get("relations")
    if not isinstance(relations, list):
        raise SystemExit("scope.relations must be an array")
    return relations


def _read_evidence_lines(lines: Iterable[str]) -> dict[str, dict[str, Any]]:
    from .candidates import pair_key

    allowed = {
        "shared_fingerprints",
        "left_fingerprints",
        "right_fingerprints",
        "jaccard",
        "containment",
        "name_token_overlap",
        "embedding_similarity",
        "query_join_count",
        "query_cooccurrence_count",
    }
    evidence: dict[str, dict[str, Any]] = {}
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        item = json.loads(line)
        left = item.get("left_population_id")
        right = item.get("right_population_id")
        values = item.get("local_evidence", {})
        if not isinstance(left, str) or not isinstance(right, str) or left == right:
            raise SystemExit(f"evidence line {line_number} requires two distinct population ids")
        if not isinstance(values, dict) or set(values) - allowed:
            raise SystemExit(f"evidence line {line_number} contains unsupported evidence keys")
        if not all(isinstance(value, (int, float)) for value in values.values()):
            raise SystemExit(f"evidence line {line_number} evidence values must be numeric")
        key = pair_key(left, right)
        merged = evidence.setdefault(key, {})
        for evidence_key, value in values.items():
            # Independent adapters may nominate the same pair. Repeating an
            # evidence kind keeps the strongest observed numeric signal rather
            # than making concatenation order significant.
            merged[evidence_key] = max(float(value), float(merged.get(evidence_key, value)))
    return evidence


def _read_evidence_stream() -> dict[str, dict[str, Any]]:
    return _read_evidence_lines(sys.stdin)


def _read_evidence_files(paths: list[str]) -> dict[str, dict[str, Any]]:
    return _read_evidence_lines(
        line for path in paths for line in Path(path).read_text().splitlines()
    )


def _select_probe_pairs(
    evidence: dict[str, dict[str, Any]],
    *,
    limit: int,
) -> list[tuple[str, str]]:
    """Bound exact probes by strongest independent nomination, then stable ID."""

    if limit < 1:
        raise SystemExit("--max-probe-pairs must be positive")

    def priority(key: str) -> tuple[float, float, float, str]:
        values = evidence[key]
        return (
            -float(values.get("query_join_count", 0.0)),
            -float(values.get("query_cooccurrence_count", 0.0)),
            -float(values.get("embedding_similarity", -1.0)),
            key,
        )

    selected: list[tuple[str, str]] = []
    for key in sorted(evidence, key=priority)[:limit]:
        left, right = key.split("\x1f")
        selected.append((left, right))
    return selected


def _summary(corpus: dict[str, Any]) -> dict[str, Any]:
    return {
        "corpus_id": corpus["corpus_id"],
        "populations": len(corpus.get("populations", [])),
        "motifs": len(corpus.get("motifs", [])),
        "correspondences": len(corpus.get("correspondences", [])),
        "reviewed_populations": sum(
            item.get("gold", {}).get("reviewed") is True for item in corpus.get("populations", [])
        ),
        "reviewed_motifs": sum(
            item.get("gold", {}).get("reviewed") is True for item in corpus.get("motifs", [])
        ),
        "reviewed_correspondences": sum(
            item.get("gold", {}).get("reviewed") is True
            for item in corpus.get("correspondences", [])
        ),
        "provenance": corpus.get("provenance", {}),
        "generation": corpus.get("generation", {}),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m business_topology",
        description="Privacy-safe, source-independent Business Topology evaluation",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    synthetic = subparsers.add_parser(
        "synthetic", help="write the client-neutral regression corpus"
    )
    synthetic.add_argument("--output", default="-")

    validate = subparsers.add_parser(
        "validate", help="validate a corpus contract and privacy boundary"
    )
    validate.add_argument("corpus")
    validate.add_argument("--require-reviewed", action="store_true")

    summarize = subparsers.add_parser(
        "summarize", help="print a corpus inventory without packet contents"
    )
    summarize.add_argument("corpus")

    candidates = subparsers.add_parser("candidates", help="build a bounded human-review pair queue")
    candidates.add_argument("corpus")
    candidates.add_argument("--output", required=True)
    candidates.add_argument("--max-pairs", type=int, default=500)
    candidates.add_argument("--max-fanout", type=int, default=12)
    candidates.add_argument("--minimum-priority", type=float, default=0.12)
    candidates.add_argument(
        "--evidence-stdin",
        action="store_true",
        help="read private aggregate/embedding pair evidence as JSONL from stdin",
    )

    label_template = subparsers.add_parser(
        "label-template",
        help="write a private review overlay template keyed by opaque item ids",
    )
    label_template.add_argument("corpus")
    label_template.add_argument("--output", required=True)

    apply_labels = subparsers.add_parser(
        "apply-labels",
        help="apply a private reviewed-label overlay to an extracted corpus",
    )
    apply_labels.add_argument("corpus")
    apply_labels.add_argument("overlay")
    apply_labels.add_argument("--output", required=True)

    evaluate = subparsers.add_parser(
        "evaluate", help="evaluate deterministic baselines on reviewed labels"
    )
    evaluate.add_argument("corpus")
    evaluate.add_argument("--output", default="-")

    candidate_recall = subparsers.add_parser(
        "candidate-recall",
        help="measure evidence-pool recall on private reviewed control pairs",
    )
    candidate_recall.add_argument("corpus")
    candidate_recall.add_argument("--evidence-stdin", action="store_true", required=True)
    candidate_recall.add_argument("--output", default="-")

    train = subparsers.add_parser(
        "train-linear",
        help="train a portable explainable floor on leakage-safe family splits",
    )
    train.add_argument("corpus")
    train.add_argument("--task", choices=("population", "correspondence"), required=True)
    train.add_argument("--checkpoint", required=True)
    train.add_argument("--report", default="-")
    train.add_argument("--test-fraction", type=float, default=0.25)
    train.add_argument("--seed", default="rvbbit-topology-v1")
    train.add_argument("--target-precision", type=float, default=0.98)

    extract = subparsers.add_parser(
        "extract-postgres",
        help="rollback-only extraction through business_topology_profile_packet",
    )
    extract.add_argument("--relation", action="append", default=[])
    extract.add_argument(
        "--scope", help="JSON scope file with relation and optional split_group entries"
    )
    extract.add_argument("--corpus-id", required=True)
    extract.add_argument("--sample-rows", type=int, default=2048)
    extract.add_argument("--statement-timeout-ms", type=int, default=120_000)
    extract.add_argument("--dsn-env", default="RVBBIT_DSN")
    extract.add_argument("--output", required=True)

    stream = subparsers.add_parser(
        "ingest-profile-stream",
        help="assemble JSONL relation profiles received over a protected transport",
    )
    stream.add_argument("--corpus-id", required=True)
    stream.add_argument("--output", required=True)

    embedding_inputs = subparsers.add_parser(
        "embedding-inputs",
        help="emit bounded provider-neutral population text as JSONL",
    )
    embedding_inputs.add_argument("corpus")
    embedding_inputs.add_argument("--max-text-chars", type=int, default=12_000)
    embedding_inputs.add_argument("--include-context", action="store_true")
    embedding_inputs.add_argument(
        "--role", action="append", default=[], help="optional structural-role filter"
    )
    embedding_inputs.add_argument(
        "--kind", action="append", default=[], help="optional population-kind filter"
    )
    embedding_inputs.add_argument(
        "--channel",
        action="append",
        default=[],
        choices=("combined", "focus", "context"),
        help="embedding text channel; repeat for a multi-channel representation",
    )
    embedding_inputs.add_argument("--output", default="-")

    embedding_evidence = subparsers.add_parser(
        "embedding-evidence",
        help="turn a private JSONL vector stream into cross-source pair evidence",
    )
    embedding_evidence.add_argument("corpus")
    embedding_evidence.add_argument("vectors", help="JSONL vectors file or - for stdin")
    embedding_evidence.add_argument("--top-k", type=int, default=32)
    embedding_evidence.add_argument("--minimum-similarity", type=float, default=0.35)
    embedding_evidence.add_argument("--block-size", type=int, default=256)
    embedding_evidence.add_argument("--output", default="-")

    source_neighborhoods = subparsers.add_parser(
        "source-neighborhoods",
        help="propose unnamed source neighborhoods and non-merging bridges",
    )
    source_neighborhoods.add_argument("corpus")
    source_neighborhoods.add_argument("vectors", help="JSONL vectors file or - for stdin")
    source_neighborhoods.add_argument("--tight-top-k", type=int, default=4)
    source_neighborhoods.add_argument("--bridge-top-k", type=int, default=6)
    source_neighborhoods.add_argument("--tight-quantile", type=float, default=0.97)
    source_neighborhoods.add_argument("--bridge-quantile", type=float, default=0.90)
    source_neighborhoods.add_argument("--minimum-tight-similarity", type=float, default=0.80)
    source_neighborhoods.add_argument("--minimum-bridge-similarity", type=float, default=0.70)
    source_neighborhoods.add_argument("--minimum-tight-local-lift", type=float, default=0.05)
    source_neighborhoods.add_argument("--minimum-bridge-local-lift", type=float, default=0.03)
    source_neighborhoods.add_argument(
        "--control-report",
        help="optional private split-group diagnostic; never used by inference",
    )
    source_neighborhoods.add_argument("--output", required=True)

    excavation_plan = subparsers.add_parser(
        "excavation-plan",
        help="build bounded motif, correspondence, synthesis, and bridge work",
    )
    excavation_plan.add_argument("corpus")
    excavation_plan.add_argument("neighborhoods")
    excavation_plan.add_argument(
        "--evidence",
        action="append",
        default=[],
        help="optional numeric evidence JSONL; repeat to merge independent adapters",
    )
    excavation_plan.add_argument("--maximum-sources-per-unit", type=int, default=24)
    excavation_plan.add_argument("--maximum-populations-per-unit", type=int, default=48)
    excavation_plan.add_argument("--max-pairs-per-unit", type=int, default=64)
    excavation_plan.add_argument("--max-pairs-per-link", type=int, default=8)
    excavation_plan.add_argument("--max-population-fanout", type=int, default=8)
    excavation_plan.add_argument("--minimum-pair-priority", type=float, default=0.32)
    excavation_plan.add_argument("--max-cross-neighborhood-links", type=int, default=100)
    excavation_plan.add_argument("--output", required=True)

    validate_result = subparsers.add_parser(
        "validate-excavation-result",
        help="validate any excavation result against its exact bounded work item",
    )
    validate_result.add_argument("plan")
    validate_result.add_argument("result")
    validate_result.add_argument(
        "--prior-result",
        action="append",
        default=[],
        help="optional validated neighborhood result used to resolve bridge node refs",
    )

    execute = subparsers.add_parser(
        "execute-excavation-plan",
        help="run a bounded proposal-only work closure through local scoring and Hutch",
    )
    execute.add_argument("plan")
    execute.add_argument("--output-dir", required=True)
    execute.add_argument("--work-id", action="append", default=[])
    execute.add_argument("--unit-id", action="append", default=[])
    execute.add_argument("--link-id", action="append", default=[])
    execute.add_argument("--all", action="store_true", dest="all_work")
    execute.add_argument("--max-work-items", type=int, default=100)
    execute.add_argument("--max-llm-calls", type=int, default=16)
    execute.add_argument("--repair-attempts", type=int, default=1)
    execute.add_argument("--backend", default="clover_llm")
    execute.add_argument("--model", default="clover")
    execute.add_argument("--request-user", default="business-topology-worker")
    execute.add_argument("--timeout-seconds", type=float, default=180.0)
    execute.add_argument("--transport-attempts", type=int, default=3)
    execute.add_argument("--dsn-env", default="RVBBIT_DSN")
    execute.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve and report the dependency closure without network or filesystem writes",
    )

    stage_bundles = subparsers.add_parser(
        "stage-proposal-bundles",
        help="persist validated synthesis receipts for review without topology promotion",
    )
    stage_bundles.add_argument("plan")
    stage_bundles.add_argument("execution_dir")
    stage_bundles.add_argument("--include-bridges", action="store_true")
    stage_bundles.add_argument("--proposed-by")
    stage_bundles.add_argument("--dsn-env", default="RVBBIT_DSN")
    stage_bundles.add_argument("--dry-run", action="store_true")

    embed_postgres = subparsers.add_parser(
        "embed-postgres-shadow",
        help="embed bounded inputs through pg_rvbbit with unconditional rollback",
    )
    embed_postgres.add_argument("corpus")
    embed_postgres.add_argument("--specialist", default="embed")
    embed_postgres.add_argument("--mode", default="document")
    embed_postgres.add_argument("--max-text-chars", type=int, default=12_000)
    embed_postgres.add_argument(
        "--role", action="append", default=[], help="optional structural-role filter"
    )
    embed_postgres.add_argument(
        "--kind", action="append", default=[], help="optional population-kind filter"
    )
    embed_postgres.add_argument(
        "--channel",
        action="append",
        default=[],
        choices=("combined", "focus", "context"),
        help="embedding text channel; repeat for a multi-channel representation",
    )
    embed_postgres.add_argument("--max-batch-items", type=int, default=16)
    embed_postgres.add_argument("--max-batch-chars", type=int, default=24_000)
    embed_postgres.add_argument("--statement-timeout-ms", type=int, default=300_000)
    embed_postgres.add_argument("--allow-partial", action="store_true")
    embed_postgres.add_argument("--dsn-env", default="RVBBIT_DSN")
    embed_postgres.add_argument("--output", required=True)

    overlap_postgres = subparsers.add_parser(
        "overlap-postgres-shadow",
        help="emit numeric local-overlap evidence with unconditional rollback",
    )
    overlap_postgres.add_argument("corpus")
    overlap_postgres.add_argument(
        "--probe-evidence",
        action="append",
        default=[],
        help=(
            "JSONL semantic/usage evidence whose bounded pairs receive exact local overlap "
            "probes; repeat for multiple files"
        ),
    )
    overlap_postgres.add_argument("--sample-rows", type=int, default=2048)
    overlap_postgres.add_argument("--min-shared", type=int, default=1)
    overlap_postgres.add_argument("--max-fingerprint-fanout", type=int, default=50)
    overlap_postgres.add_argument("--max-pairs", type=int, default=50_000)
    overlap_postgres.add_argument("--max-probe-pairs", type=int, default=10_000)
    overlap_postgres.add_argument("--statement-timeout-ms", type=int, default=300_000)
    overlap_postgres.add_argument("--dsn-env", default="RVBBIT_DSN")
    overlap_postgres.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "synthetic":
        corpus = make_synthetic_corpus()
        _write_json(args.output, corpus)
        return 0
    if args.command == "validate":
        corpus = load_corpus(args.corpus, require_reviewed=args.require_reviewed)
        _write_json("-", {"valid": True, **_summary(corpus)})
        return 0
    if args.command == "summarize":
        _write_json("-", _summary(load_corpus(args.corpus)))
        return 0
    if args.command == "candidates":
        corpus = load_corpus(args.corpus)
        evidence = _read_evidence_stream() if args.evidence_stdin else None
        if args.evidence_stdin and not evidence:
            raise SystemExit("--evidence-stdin received no evidence rows")
        queued = build_candidate_queue(
            corpus,
            max_pairs=args.max_pairs,
            max_fanout=args.max_fanout,
            minimum_priority=args.minimum_priority,
            evidence_by_pair=evidence,
        )
        write_corpus(args.output, queued)
        _write_json("-", _summary(queued))
        return 0
    if args.command == "label-template":
        _write_json(args.output, make_label_template(load_corpus(args.corpus)))
        return 0
    if args.command == "apply-labels":
        corpus = load_corpus(args.corpus)
        overlay = json.loads(Path(args.overlay).read_text())
        labeled, applied = apply_label_overlay(corpus, overlay)
        write_corpus(args.output, labeled)
        _write_json("-", {"corpus": _summary(labeled), "labels_applied": applied})
        return 0
    if args.command == "evaluate":
        report = evaluate_corpus(load_corpus(args.corpus, require_reviewed=True))
        _write_json(args.output, report)
        return 0
    if args.command == "candidate-recall":
        corpus = load_corpus(args.corpus)
        evidence = _read_evidence_stream()
        if not evidence:
            raise SystemExit("--evidence-stdin received no evidence rows")
        _write_json(args.output, evaluate_candidate_recall(corpus, evidence))
        return 0
    if args.command == "train-linear":
        checkpoint, report = train_corpus_baseline(
            load_corpus(args.corpus, require_reviewed=True),
            task=args.task,
            test_fraction=args.test_fraction,
            seed=args.seed,
            target_precision=args.target_precision,
        )
        Path(args.checkpoint).write_text(checkpoint.dumps())
        _write_json(args.report, report)
        return 0
    if args.command == "extract-postgres":
        relations: list[str | dict[str, Any]] = list(args.relation)
        if args.scope:
            relations.extend(_load_scope(args.scope))
        with connect_from_env(args.dsn_env) as conn:
            corpus, audit = extract_postgres_shadow_corpus(
                conn,
                relations,
                corpus_id=args.corpus_id,
                sample_rows=args.sample_rows,
                statement_timeout_ms=args.statement_timeout_ms,
            )
        write_corpus(args.output, corpus)
        _write_json("-", {"corpus": _summary(corpus), "shadow_audit": audit})
        return 0
    if args.command == "ingest-profile-stream":
        items = [json.loads(line) for line in sys.stdin if line.strip()]
        corpus = corpus_from_relation_profiles(
            items,
            corpus_id=args.corpus_id,
            provenance={
                "transport": "jsonl_stdin",
                "persistent_topology_writes": False,
            },
        )
        write_corpus(args.output, corpus)
        _write_json("-", _summary(corpus))
        return 0
    if args.command == "embedding-inputs":
        corpus = load_corpus(args.corpus)
        inputs = make_embedding_inputs(
            corpus,
            max_text_chars=args.max_text_chars,
            include_context=args.include_context,
            roles=args.role,
            channels=args.channel or ("combined",),
            kinds=args.kind,
        )
        _write_jsonl(args.output, inputs)
        return 0
    if args.command == "embedding-evidence":
        corpus = load_corpus(args.corpus)
        if args.vectors == "-":
            rows = [json.loads(line) for line in sys.stdin if line.strip()]
        else:
            rows = [
                json.loads(line)
                for line in Path(args.vectors).read_text().splitlines()
                if line.strip()
            ]
        evidence = build_embedding_evidence(
            corpus,
            rows,
            top_k=args.top_k,
            minimum_similarity=args.minimum_similarity,
            block_size=args.block_size,
        )
        _write_jsonl(args.output, evidence)
        return 0
    if args.command == "source-neighborhoods":
        corpus = load_corpus(args.corpus)
        if args.vectors == "-":
            rows = [json.loads(line) for line in sys.stdin if line.strip()]
        else:
            rows = [
                json.loads(line)
                for line in Path(args.vectors).read_text().splitlines()
                if line.strip()
            ]
        proposal = propose_source_neighborhoods(
            corpus,
            rows,
            tight_top_k=args.tight_top_k,
            bridge_top_k=args.bridge_top_k,
            tight_quantile=args.tight_quantile,
            bridge_quantile=args.bridge_quantile,
            minimum_tight_similarity=args.minimum_tight_similarity,
            minimum_bridge_similarity=args.minimum_bridge_similarity,
            minimum_tight_local_lift=args.minimum_tight_local_lift,
            minimum_bridge_local_lift=args.minimum_bridge_local_lift,
        )
        _write_json(args.output, proposal)
        result: dict[str, Any] = {"source_neighborhoods": proposal["summary"]}
        if args.control_report:
            controls = evaluate_source_neighborhood_controls(corpus, proposal)
            _write_json(args.control_report, controls)
            result["control_diagnostics"] = {
                key: value
                for key, value in controls.items()
                if key not in {"cross_control_tight_edges", "warning"}
            }
        _write_json("-", result)
        return 0
    if args.command == "excavation-plan":
        corpus = load_corpus(args.corpus)
        neighborhoods = json.loads(Path(args.neighborhoods).read_text())
        evidence = _read_evidence_files(args.evidence) if args.evidence else None
        plan = build_excavation_plan(
            corpus,
            neighborhoods,
            evidence_by_pair=evidence,
            maximum_sources_per_unit=args.maximum_sources_per_unit,
            maximum_populations_per_unit=args.maximum_populations_per_unit,
            max_pairs_per_unit=args.max_pairs_per_unit,
            max_pairs_per_link=args.max_pairs_per_link,
            max_population_fanout=args.max_population_fanout,
            minimum_pair_priority=args.minimum_pair_priority,
            max_cross_neighborhood_links=args.max_cross_neighborhood_links,
        )
        _write_json(args.output, plan)
        _write_json("-", {"excavation_plan": plan["summary"]})
        return 0
    if args.command == "validate-excavation-result":
        plan = json.loads(Path(args.plan).read_text())
        loaded_result: Any = json.loads(Path(args.result).read_text())
        if loaded_result.get("schema_version") == WORK_RECEIPT_SCHEMA_VERSION:
            loaded_result = loaded_result.get("result")
        if not isinstance(loaded_result, dict):
            raise SystemExit("result file must contain an excavation result object")
        prior_results: dict[str, dict[str, Any]] = {}
        for path in args.prior_result:
            prior: Any = json.loads(Path(path).read_text())
            if prior.get("schema_version") == WORK_RECEIPT_SCHEMA_VERSION:
                prior = prior.get("result")
            work_id = prior.get("work_id") if isinstance(prior, dict) else None
            if not isinstance(work_id, str) or not work_id or work_id in prior_results:
                raise SystemExit("every --prior-result needs a unique non-empty work_id")
            prior_results[work_id] = prior
        _write_json(
            "-",
            validate_excavation_result(
                plan,
                loaded_result,
                prior_results=prior_results,
            ),
        )
        return 0
    if args.command == "execute-excavation-plan":
        if (
            args.max_work_items < 1
            or args.max_llm_calls < 0
            or args.repair_attempts < 0
            or args.transport_attempts < 1
        ):
            raise SystemExit("execution limits must be non-negative and max-work-items positive")
        plan = json.loads(Path(args.plan).read_text())
        selected = select_work(
            plan,
            work_ids=args.work_id,
            unit_ids=args.unit_id,
            link_ids=args.link_id,
            all_work=args.all_work,
        )
        preview = execution_preview(plan, selected)
        if args.dry_run:
            _write_json("-", preview)
            return 0
        dsn = os.environ.get(args.dsn_env)
        if not dsn:
            raise SystemExit(f"{args.dsn_env} is not set")
        client = HutchChatClient.from_postgres(
            dsn,
            backend_name=args.backend,
            model=args.model,
            request_user=args.request_user,
            timeout_seconds=args.timeout_seconds,
            transport_attempts=args.transport_attempts,
        )
        summary = execute_plan(
            plan,
            selected,
            output_dir=args.output_dir,
            client=client,
            max_work_items=args.max_work_items,
            max_llm_calls=args.max_llm_calls,
            repair_attempts=args.repair_attempts,
        )
        _write_json("-", {"preview": preview, "execution": summary})
        return 0
    if args.command == "stage-proposal-bundles":
        plan = json.loads(Path(args.plan).read_text())
        if args.dry_run:
            summary = stage_execution_bundles(
                None,
                plan,
                args.execution_dir,
                include_bridges=args.include_bridges,
                proposed_by=args.proposed_by,
                dry_run=True,
            )
        else:
            with connect_from_env(args.dsn_env) as conn:
                summary = stage_execution_bundles(
                    conn,
                    plan,
                    args.execution_dir,
                    include_bridges=args.include_bridges,
                    proposed_by=args.proposed_by,
                )
        _write_json("-", {"proposal_bundles": summary})
        return 0
    if args.command == "embed-postgres-shadow":
        corpus = load_corpus(args.corpus)
        inputs = make_embedding_inputs(
            corpus,
            max_text_chars=args.max_text_chars,
            roles=args.role,
            channels=args.channel or ("combined",),
            kinds=args.kind,
        )
        with connect_from_env(args.dsn_env) as conn:
            vectors, audit = embed_postgres_shadow(
                conn,
                inputs,
                specialist=args.specialist,
                mode=args.mode,
                max_batch_items=args.max_batch_items,
                max_batch_chars=args.max_batch_chars,
                statement_timeout_ms=args.statement_timeout_ms,
            )
        if audit["failures"] and not args.allow_partial:
            raise SystemExit(
                f"embedding failed for {len(audit['failures'])} populations; "
                "no vector file was written (use --allow-partial to retain successes)"
            )
        _write_jsonl(args.output, vectors)
        _write_json("-", {"embedding_shadow_audit": audit})
        return 0
    if args.command == "overlap-postgres-shadow":
        corpus = load_corpus(args.corpus)
        probe_evidence = _read_evidence_files(args.probe_evidence) if args.probe_evidence else {}
        probe_pairs = _select_probe_pairs(
            probe_evidence,
            limit=args.max_probe_pairs,
        )
        with connect_from_env(args.dsn_env) as conn:
            evidence, audit = extract_postgres_overlap_shadow(
                conn,
                corpus,
                probe_pairs=probe_pairs,
                sample_rows=args.sample_rows,
                min_shared=args.min_shared,
                max_fingerprint_fanout=args.max_fingerprint_fanout,
                max_pairs=args.max_pairs,
                max_probe_pairs=args.max_probe_pairs,
                statement_timeout_ms=args.statement_timeout_ms,
            )
        audit["nominated_probe_pairs"] = len(probe_evidence)
        audit["probe_pairs_truncated"] = max(len(probe_evidence) - len(probe_pairs), 0)
        _write_jsonl(args.output, evidence)
        _write_json("-", {"overlap_shadow_audit": audit})
        return 0
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
