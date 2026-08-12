"""Rollback-only PostgreSQL shadow extraction for topology evaluation."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from typing import Any

from .candidates import pair_key
from .contracts import CORPUS_SCHEMA_VERSION, ContractError, validate_corpus
from .packets import expand_postgres_relation_profile


LEDGER_TABLES = (
    "business_topology_sources",
    "business_topology_populations",
    "business_topology_profile_snapshots",
    "business_topology_inference_jobs",
    "business_topology_proposals",
    "business_topology_nodes",
)


def corpus_from_relation_profiles(
    profile_items: Sequence[Mapping[str, Any]],
    *,
    corpus_id: str,
    provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble one corpus from adapter-neutral relation-profile records."""

    populations: list[dict[str, Any]] = []
    motifs: list[dict[str, Any]] = []
    relations: list[str] = []
    for index, item in enumerate(profile_items):
        packet = item.get("packet")
        if not isinstance(packet, Mapping):
            raise ContractError(f"profile item {index} requires a packet object")
        relation = item.get("relation")
        if not isinstance(relation, str) or not relation:
            source = packet.get("source", {})
            relation = (
                f"{source.get('schema')}.{source.get('relation')}"
                if isinstance(source, Mapping)
                else f"profile-{index}"
            )
        split_group = item.get("split_group")
        expanded = expand_postgres_relation_profile(
            packet,
            split_group=split_group if isinstance(split_group, str) else None,
        )
        populations.extend(expanded["populations"])
        motifs.extend(expanded["motifs"])
        relations.append(relation)
    corpus = {
        "schema_version": CORPUS_SCHEMA_VERSION,
        "corpus_id": corpus_id,
        "description": "Privacy-safe source profile corpus",
        "populations": populations,
        "motifs": motifs,
        "correspondences": [],
        "provenance": {
            "kind": "profile_stream",
            "relations_profiled": relations,
            "contains_raw_values": False,
            "contains_value_hashes": False,
            **dict(provenance or {}),
        },
    }
    validate_corpus(corpus)
    return corpus


def _ledger_counts(conn: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    with conn.cursor() as cursor:
        for table in LEDGER_TABLES:
            cursor.execute(f"SELECT count(*) FROM rvbbit.{table}".encode())  # type: ignore[arg-type]
            counts[table] = int(cursor.fetchone()[0])
    conn.rollback()
    return counts


def _scope_item(value: str | Mapping[str, Any]) -> tuple[str, str | None]:
    if isinstance(value, str):
        relation = value
        split_group = None
    elif isinstance(value, Mapping):
        relation = value.get("relation")
        split_group = value.get("split_group")
    else:
        raise ContractError("scope relation entries must be strings or objects")
    if not isinstance(relation, str) or not relation.strip():
        raise ContractError("every scope relation requires a non-empty relation name")
    if split_group is not None and (not isinstance(split_group, str) or not split_group.strip()):
        raise ContractError("scope split_group must be a non-empty string when supplied")
    return relation, split_group


def extract_postgres_shadow_corpus(
    conn: Any,
    relations: Sequence[str | Mapping[str, Any]],
    *,
    corpus_id: str,
    sample_rows: int = 2048,
    statement_timeout_ms: int = 120_000,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Profile relations without inserting any persistent topology state.

    ``business_topology_profile_packet`` uses transaction-local temporary
    tables, so PostgreSQL cannot execute it inside ``BEGIN READ ONLY``.  This
    function instead uses a normal transaction, calls only the non-persisting
    packet function, and always rolls the transaction back.  A hardened shadow
    role should have SELECT on scoped sources and TEMP on the database, but no
    DML privileges on RVBBIT or customer schemas.
    """

    if not relations:
        raise ContractError("at least one relation is required")
    if not 32 <= sample_rows <= 50_000:
        raise ContractError("sample_rows must be between 32 and 50000")
    if not corpus_id.strip():
        raise ContractError("corpus_id must be non-empty")

    before = _ledger_counts(conn)
    profile_items: list[dict[str, Any]] = []
    profiled: list[str] = []
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"SET LOCAL statement_timeout={int(statement_timeout_ms)}".encode())  # type: ignore[arg-type]
            cursor.execute(b"SET LOCAL rvbbit.force_heap_scan='on'")
            for raw_item in relations:
                relation, split_group = _scope_item(raw_item)
                cursor.execute("SELECT to_regclass(%s)::text", (relation,))
                resolved_row = cursor.fetchone()
                resolved = resolved_row[0] if resolved_row is not None else None
                if resolved is None:
                    raise ContractError(f"relation does not exist or is not visible: {relation}")
                cursor.execute(
                    "SELECT rvbbit.business_topology_profile_packet(%s::regclass,%s)",
                    (relation, sample_rows),
                )
                relation_packet = cursor.fetchone()[0]
                profile_items.append(
                    {
                        "relation": str(resolved),
                        "split_group": split_group,
                        "packet": relation_packet,
                    }
                )
                profiled.append(str(resolved))
    finally:
        # This is the central safety property: even an adapter regression or a
        # failed relation leaves no transaction capable of committing.
        conn.rollback()

    after = _ledger_counts(conn)
    unchanged = before == after
    if not unchanged:
        raise RuntimeError(
            "topology ledger counts changed during rollback-only shadow extraction; "
            "discard the corpus and investigate concurrent or adapter writes"
        )
    corpus = corpus_from_relation_profiles(
        profile_items,
        corpus_id=corpus_id,
        provenance={
            "kind": "postgres_shadow",
            "relations_profiled": profiled,
            "sample_rows": sample_rows,
            "contains_raw_values": False,
            "contains_value_hashes": False,
            "persistent_topology_writes": False,
            "transaction_rolled_back": True,
            "ledger_counts_unchanged": True,
        },
    )
    return corpus, {
        "relations": len(profiled),
        "populations": len(corpus["populations"]),
        "motifs": len(corpus["motifs"]),
        "ledger_before": before,
        "ledger_after": after,
        "ledger_unchanged": unchanged,
    }


def extract_postgres_overlap_shadow(
    conn: Any,
    corpus: Mapping[str, Any],
    *,
    probe_pairs: Sequence[tuple[str, str]] = (),
    sample_rows: int = 2048,
    min_shared: int = 1,
    max_fingerprint_fanout: int = 50,
    max_pairs: int = 50_000,
    max_probe_pairs: int = 10_000,
    statement_timeout_ms: int = 300_000,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return numeric local-overlap evidence with no durable topology writes.

    PostgreSQL is merely one private evidence adapter. Raw values and salted
    fingerprints stay inside the transaction; only aggregate counts and scores
    cross into the source-independent evaluator.
    """

    validate_corpus(corpus)
    if not 32 <= sample_rows <= 50_000:
        raise ContractError("sample_rows must be between 32 and 50000")
    if min_shared < 1 or max_fingerprint_fanout < 2 or max_pairs < 1 or max_probe_pairs < 1:
        raise ContractError("overlap bounds must be positive and fanout at least two")

    relations: set[tuple[str, str, str]] = set()
    population_keys: set[str] = set()
    for item in corpus.get("populations", []):
        packet = item["packet"]
        source = packet.get("source", {})
        if source.get("kind") != "postgres_relation":
            continue
        database = source.get("database")
        schema = source.get("schema")
        relation = source.get("relation")
        if not all(isinstance(value, str) and value for value in (database, schema, relation)):
            raise ContractError("PostgreSQL packets require database, schema, and relation names")
        relations.add((database, schema, relation))
        if packet.get("population", {}).get("kind") == "field":
            population_keys.add(str(item["population_id"]))
    if not relations or len(population_keys) < 2:
        raise ContractError("corpus requires at least two PostgreSQL field populations")

    normalized_probe_pairs: list[tuple[str, str]] = []
    for raw_left, raw_right in probe_pairs:
        left, right = sorted((str(raw_left), str(raw_right)))
        if left == right:
            raise ContractError("overlap probe pairs require two distinct populations")
        if left not in population_keys or right not in population_keys:
            raise ContractError("overlap probe pair references a non-PostgreSQL field population")
        normalized_probe_pairs.append((left, right))
    normalized_probe_pairs = sorted(set(normalized_probe_pairs))
    if len(normalized_probe_pairs) > max_probe_pairs:
        raise ContractError(
            f"overlap probe pair count {len(normalized_probe_pairs)} exceeds {max_probe_pairs}"
        )

    before = _ledger_counts(conn)
    rows: list[tuple[Any, ...]] = []
    global_pair_keys: set[str] = set()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"SET LOCAL statement_timeout={int(statement_timeout_ms)}".encode())  # type: ignore[arg-type]
            cursor.execute(b"SET LOCAL rvbbit.force_heap_scan='on'")
            cursor.execute("SELECT current_database()")
            current_database = str(cursor.fetchone()[0])
            for database, schema, relation in sorted(relations):
                if database != current_database:
                    raise ContractError(
                        f"corpus source database {database} does not match {current_database}"
                    )
                cursor.execute(
                    """
                    SELECT c.oid::regclass::text
                    FROM pg_catalog.pg_class c
                    JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname=%s AND c.relname=%s
                    """,
                    (schema, relation),
                )
                resolved_row = cursor.fetchone()
                resolved = resolved_row[0] if resolved_row is not None else None
                if resolved is None:
                    raise ContractError(
                        f"relation does not exist or is not visible: {schema}.{relation}"
                    )
                cursor.execute(
                    "SELECT rvbbit.business_topology_excavate_relation(%s::regclass,%s,false,false)",
                    (resolved, sample_rows),
                )
                cursor.fetchone()

            cursor.execute(
                """
                WITH scoped_populations AS (
                    SELECT p.population_id,p.population_key,p.display_name,
                           p.current_profile_id,
                           coalesce(ps.model_packet #>> '{field,name}',p.display_name) AS field_name
                    FROM rvbbit.business_topology_populations p
                    JOIN rvbbit.business_topology_profile_snapshots ps
                      ON ps.profile_id=p.current_profile_id
                    WHERE p.population_key=ANY(%s::text[])
                      AND p.status='active'
                      AND p.population_kind='field'
                ), current_values AS (
                    SELECT p.population_id,p.population_key,p.display_name,
                           p.field_name,vf.fingerprint
                    FROM scoped_populations p
                    JOIN rvbbit.business_topology_value_fingerprints vf
                      ON vf.population_id=p.population_id
                     AND vf.profile_id=p.current_profile_id
                ), useful_fingerprints AS (
                    SELECT fingerprint
                    FROM current_values
                    GROUP BY fingerprint
                    HAVING count(DISTINCT population_id) BETWEEN 2 AND %s
                ), population_counts AS (
                    SELECT population_id,count(DISTINCT fingerprint)::bigint AS n
                    FROM current_values
                    GROUP BY population_id
                ), pairs AS (
                    SELECT l.population_id AS left_id,
                           r.population_id AS right_id,
                           min(l.population_key) AS left_key,
                           min(r.population_key) AS right_key,
                           min(l.field_name) AS left_field_name,
                           min(r.field_name) AS right_field_name,
                           count(DISTINCT l.fingerprint)::bigint AS shared
                    FROM current_values l
                    JOIN useful_fingerprints u USING (fingerprint)
                    JOIN current_values r
                      ON r.fingerprint=l.fingerprint
                     AND r.population_id>l.population_id
                    GROUP BY l.population_id,r.population_id
                    HAVING count(DISTINCT l.fingerprint)>=%s
                )
                SELECT pairs.left_key,pairs.right_key,pairs.shared,lc.n,rc.n,
                       pairs.shared::float8/nullif(lc.n+rc.n-pairs.shared,0)::float8 AS jaccard,
                       greatest(
                         pairs.shared::float8/nullif(lc.n,0),
                         pairs.shared::float8/nullif(rc.n,0)
                       ) AS containment,
                       rvbbit._business_topology_token_overlap(
                         pairs.left_field_name,pairs.right_field_name
                       ) AS name_token_overlap
                FROM pairs
                JOIN population_counts lc ON lc.population_id=pairs.left_id
                JOIN population_counts rc ON rc.population_id=pairs.right_id
                ORDER BY containment DESC,jaccard DESC,pairs.shared DESC,
                         pairs.left_key,pairs.right_key
                LIMIT %s
                """,
                (sorted(population_keys), max_fingerprint_fanout, min_shared, max_pairs),
            )
            rows = cursor.fetchall()
            global_pair_keys = {pair_key(str(row[0]), str(row[1])) for row in rows}

            # The inverted-index pass above intentionally suppresses values
            # that occur across many populations. That keeps common booleans,
            # statuses, and tiny integers from exploding the candidate graph,
            # but it can also hide a real key pair such as two region-code
            # fields. Semantic/usage adapters may nominate a bounded set of
            # pairs for an exact local probe. This bypasses only the global
            # fingerprint-fanout filter; it does not expose values or expand
            # into an all-pairs comparison.
            if normalized_probe_pairs:
                cursor.execute(
                    """
                    WITH requested_pairs AS (
                        SELECT left_key,right_key
                        FROM jsonb_to_recordset(%s::jsonb)
                             AS requested(left_key text,right_key text)
                    ), scoped_populations AS (
                        SELECT p.population_id,p.population_key,
                               p.current_profile_id,
                               coalesce(
                                   ps.model_packet #>> '{field,name}',p.display_name
                               ) AS field_name
                        FROM rvbbit.business_topology_populations p
                        JOIN rvbbit.business_topology_profile_snapshots ps
                          ON ps.profile_id=p.current_profile_id
                        WHERE p.population_key=ANY(%s::text[])
                          AND p.status='active'
                          AND p.population_kind='field'
                    ), pair_profiles AS (
                        SELECT requested.left_key,requested.right_key,
                               left_population.current_profile_id AS left_profile_id,
                               right_population.current_profile_id AS right_profile_id,
                               left_population.field_name AS left_field_name,
                               right_population.field_name AS right_field_name
                        FROM requested_pairs requested
                        JOIN scoped_populations left_population
                          ON left_population.population_key=requested.left_key
                        JOIN scoped_populations right_population
                          ON right_population.population_key=requested.right_key
                    ), population_counts AS (
                        SELECT profile_id,count(DISTINCT fingerprint)::bigint AS n
                        FROM rvbbit.business_topology_value_fingerprints
                        WHERE profile_id IN (
                            SELECT left_profile_id FROM pair_profiles
                            UNION
                            SELECT right_profile_id FROM pair_profiles
                        )
                        GROUP BY profile_id
                    ), pair_overlaps AS (
                        SELECT pair_profiles.left_key,pair_profiles.right_key,
                               pair_profiles.left_profile_id,
                               pair_profiles.right_profile_id,
                               pair_profiles.left_field_name,
                               pair_profiles.right_field_name,
                               count(DISTINCT left_values.fingerprint)::bigint AS shared
                        FROM pair_profiles
                        JOIN rvbbit.business_topology_value_fingerprints left_values
                          ON left_values.profile_id=pair_profiles.left_profile_id
                        JOIN rvbbit.business_topology_value_fingerprints right_values
                          ON right_values.profile_id=pair_profiles.right_profile_id
                         AND right_values.fingerprint=left_values.fingerprint
                        GROUP BY pair_profiles.left_key,pair_profiles.right_key,
                                 pair_profiles.left_profile_id,
                                 pair_profiles.right_profile_id,
                                 pair_profiles.left_field_name,
                                 pair_profiles.right_field_name
                       HAVING count(DISTINCT left_values.fingerprint)>=%s
                    )
                    SELECT pair_match.left_key,pair_match.right_key,pair_match.shared,
                           left_counts.n,right_counts.n,
                           pair_match.shared::float8
                             / nullif(
                                 left_counts.n+right_counts.n-pair_match.shared,0
                               )::float8 AS jaccard,
                           greatest(
                               pair_match.shared::float8/nullif(left_counts.n,0),
                               pair_match.shared::float8/nullif(right_counts.n,0)
                           ) AS containment,
                           rvbbit._business_topology_token_overlap(
                               pair_match.left_field_name,pair_match.right_field_name
                           ) AS name_token_overlap
                    FROM pair_overlaps pair_match
                    JOIN population_counts left_counts
                      ON left_counts.profile_id=pair_match.left_profile_id
                    JOIN population_counts right_counts
                      ON right_counts.profile_id=pair_match.right_profile_id
                    ORDER BY containment DESC,jaccard DESC,pair_match.shared DESC,
                             pair_match.left_key,pair_match.right_key
                    """,
                    (
                        json.dumps(
                            [
                                {"left_key": left, "right_key": right}
                                for left, right in normalized_probe_pairs
                            ]
                        ),
                        sorted(population_keys),
                        min_shared,
                    ),
                )
                rows.extend(cursor.fetchall())
    finally:
        conn.rollback()

    after = _ledger_counts(conn)
    if before != after:
        raise RuntimeError("topology ledger counts changed during rollback-only overlap extraction")
    evidence: dict[str, dict[str, Any]] = {}
    for left, right, shared, left_count, right_count, jaccard, containment, name_overlap in rows:
        left_id, right_id = sorted((str(left), str(right)))
        evidence[pair_key(left_id, right_id)] = {
            "left_population_id": left_id,
            "right_population_id": right_id,
            "local_evidence": {
                "shared_fingerprints": int(shared),
                "left_fingerprints": int(left_count),
                "right_fingerprints": int(right_count),
                "jaccard": float(jaccard or 0.0),
                "containment": float(containment or 0.0),
                "name_token_overlap": float(name_overlap or 0.0),
            },
        }
    probed_keys = {pair_key(left, right) for left, right in normalized_probe_pairs}
    return list(evidence.values()), {
        "relations": len(relations),
        "populations": len(population_keys),
        "candidate_pairs": len(evidence),
        "global_candidate_pairs": len(global_pair_keys),
        "targeted_probe_pairs": len(normalized_probe_pairs),
        "targeted_overlap_pairs": len(set(evidence) & probed_keys),
        "sample_rows": sample_rows,
        "min_shared": min_shared,
        "max_fingerprint_fanout": max_fingerprint_fanout,
        "transaction_rolled_back": True,
        "ledger_counts_unchanged": True,
    }


def connect_from_env(env_name: str = "RVBBIT_DSN") -> Any:
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - exercised by CLI installations
        raise RuntimeError("psycopg is required for PostgreSQL shadow extraction") from exc
    dsn = os.environ.get(env_name)
    if not dsn:
        raise RuntimeError(f"set {env_name}; DSNs are intentionally not accepted as CLI arguments")
    return psycopg.connect(dsn)
