from __future__ import annotations

import json
import uuid

import pytest
from psycopg import sql


def test_business_topology_profiles_data_without_persisting_raw_values(rvbbit):
    suffix = uuid.uuid4().hex[:10]
    left_table = f"bt_records_{suffix}"
    right_table = f"bt_contacts_{suffix}"
    left_rel = f"public.{left_table}"
    right_rel = f"public.{right_table}"
    concept_name = f"Party {suffix}"
    node_proposal_key = f"test-node:{suffix}"
    binding_proposal_key = f"test-binding:{suffix}"
    population_proposal_key = f"test-population:{suffix}"
    secret_email = f"alice-{suffix}@example.test"
    source_ids: list[uuid.UUID] = []
    proposal_ids: list[uuid.UUID] = []

    try:
        rvbbit.execute(
            sql.SQL(
                """
                CREATE TABLE {} (
                    party_pk bigint,
                    full_name text,
                    email_address text,
                    current_status text,
                    activity_code text,
                    risk_score numeric,
                    joined_at timestamptz,
                    notes jsonb
                )
                """
            ).format(sql.Identifier(left_table))
        )
        rvbbit.execute(
            sql.SQL(
                """
                CREATE TABLE {} (
                    contact_ref text,
                    display_name text,
                    email text,
                    lifecycle_stage text,
                    offering_code text,
                    territory text,
                    last_touch_at timestamptz
                )
                """
            ).format(sql.Identifier(right_table))
        )
        with rvbbit.cursor() as cursor:
            cursor.executemany(
                sql.SQL("INSERT INTO {} VALUES (%s,%s,%s,%s,%s,%s,now(),%s)").format(
                    sql.Identifier(left_table)
                ),
                [
                    (
                        101,
                        "Alice Rivera",
                        secret_email,
                        "active",
                        "ACT-101",
                        0.12,
                        json.dumps({"source": "meeting"}),
                    ),
                    (
                        102,
                        "Bob Chen",
                        f"bob-{suffix}@example.test",
                        "prospect",
                        "ACT-201",
                        0.71,
                        json.dumps({"source": "system-a"}),
                    ),
                    (
                        103,
                        "Casey Jones",
                        f"casey-{suffix}@example.test",
                        "active",
                        "ACT-101",
                        0.28,
                        json.dumps({"source": "system-b"}),
                    ),
                ],
            )
            cursor.executemany(
                sql.SQL("INSERT INTO {} VALUES (%s,%s,%s,%s,%s,%s,now())").format(
                    sql.Identifier(right_table)
                ),
                [
                    (
                        "C-900",
                        "Alice Rivera",
                        secret_email,
                        "customer",
                        "ACT-101",
                        "east",
                    ),
                    (
                        "C-901",
                        "Bob Chen",
                        f"bob-{suffix}@example.test",
                        "lead",
                        "ACT-201",
                        "west",
                    ),
                    (
                        "C-902",
                        "Casey Jones",
                        f"casey-{suffix}@example.test",
                        "customer",
                        "ACT-101",
                        "east",
                    ),
                ],
            )
        rvbbit.execute(sql.SQL("ANALYZE {}").format(sql.Identifier(left_table)))
        rvbbit.execute(sql.SQL("ANALYZE {}").format(sql.Identifier(right_table)))

        packet = rvbbit.execute(
            "SELECT rvbbit.business_topology_profile_packet(%s::regclass,64)",
            (left_rel,),
        ).fetchone()[0]
        packet_text = json.dumps(packet)
        assert packet["schema_version"] == "rvbbit.business-topology.profile-packet.v1"
        assert packet["privacy"] == {
            "raw_values": False,
            "value_hashes": False,
            "bounded_sample": True,
        }
        assert packet["relation_context"]["field_count"] == 8
        assert secret_email not in packet_text
        email_profile = next(
            field
            for field in packet["relation_context"]["fields"]
            if field["name"] == "email_address"
        )
        assert email_profile["sensitivity_hint"] == "direct_identifier"
        assert email_profile["value_shapes"] == {"email": 3}
        identity_pair = next(
            pair
            for pair in packet["relation_context"]["field_pairs"]
            if pair["left"] == "full_name" and pair["right"] == "email_address"
        )
        assert identity_pair["both_present_fraction"] == 1.0
        assert identity_pair["left_to_right_strength"] == 1.0
        assert identity_pair["right_to_left_strength"] == 1.0
        assert identity_pair["equal_value_fraction"] == 0.0

        left_result = rvbbit.execute(
            "SELECT rvbbit.business_topology_excavate_relation(%s::regclass,64,false,false)",
            (left_rel,),
        ).fetchone()[0]
        right_result = rvbbit.execute(
            "SELECT rvbbit.business_topology_excavate_relation(%s::regclass,64,false,false)",
            (right_rel,),
        ).fetchone()[0]
        source_ids.extend(
            [uuid.UUID(left_result["source_id"]), uuid.UUID(right_result["source_id"])]
        )
        assert left_result["changed"] is True
        assert left_result["populations_seen"] == 9  # eight fields + context
        assert right_result["populations_seen"] == 8

        leak_count = rvbbit.execute(
            """
            SELECT count(*)
              FROM rvbbit.business_topology_profile_snapshots ps
              JOIN rvbbit.business_topology_populations p USING (population_id)
             WHERE p.source_id=ANY(%s)
               AND (ps.profile::text ILIKE %s OR ps.model_packet::text ILIKE %s)
            """,
            (source_ids, f"%{secret_email}%", f"%{secret_email}%"),
        ).fetchone()[0]
        assert leak_count == 0

        queued = rvbbit.execute(
            "SELECT rvbbit.business_topology_excavate_relation(%s::regclass,64,true,false)",
            (left_rel,),
        ).fetchone()[0]
        assert queued["changed"] is False
        assert queued["jobs_enqueued"] == 10  # nine embeddings + one motif task
        motif_packet = rvbbit.execute(
            """
            SELECT j.input_packet
              FROM rvbbit.business_topology_inference_jobs j
              JOIN rvbbit.business_topology_populations p
                ON p.population_id=j.population_id
             WHERE p.source_id=%s AND j.task_kind='source_motifs'
            """,
            (source_ids[0],),
        ).fetchone()[0]
        assert (
            motif_packet["schema_version"]
            == "rvbbit.business-topology.source-motifs.v1"
        )
        assert (
            motif_packet["output_contract"]["allow_multiple_objects_per_source"] is True
        )
        assert motif_packet["relation_context"]["field_pairs"]
        assert secret_email not in json.dumps(motif_packet)

        unchanged = rvbbit.execute(
            "SELECT rvbbit.business_topology_excavate_relation(%s::regclass,64,false,false)",
            (left_rel,),
        ).fetchone()[0]
        assert unchanged["changed"] is False
        assert unchanged["profiles_created"] == 0

        left_population = rvbbit.execute(
            """
            SELECT population_id
              FROM rvbbit.business_topology_populations
             WHERE display_name=%s AND status='active'
            """,
            (f"{left_rel}.email_address",),
        ).fetchone()[0]
        right_population = rvbbit.execute(
            """
            SELECT population_id
              FROM rvbbit.business_topology_populations
             WHERE display_name=%s AND status='active'
            """,
            (f"{right_rel}.email",),
        ).fetchone()[0]

        overlap = rvbbit.execute(
            """
            SELECT shared_fingerprints,jaccard,containment
              FROM rvbbit.business_topology_overlap_candidates(2,20,1000)
             WHERE (left_population_id=%s AND right_population_id=%s)
                OR (left_population_id=%s AND right_population_id=%s)
            """,
            (left_population, right_population, right_population, left_population),
        ).fetchone()
        assert overlap is not None
        assert overlap[0] == 3
        assert overlap[1] == 1.0
        assert overlap[2] == 1.0

        correspondence_packet = rvbbit.execute(
            "SELECT rvbbit.business_topology_correspondence_packet(%s,%s)",
            (left_population, right_population),
        ).fetchone()[0]
        correspondence_text = json.dumps(correspondence_packet)
        assert secret_email not in correspondence_text
        assert correspondence_packet["privacy"]["value_hashes"] is False
        assert "same_concept" in correspondence_packet["verdict_contract"]
        assert "same_instance_key" in correspondence_packet["verdict_contract"]
        assert correspondence_packet["local_evidence"]["shared_fingerprints"] == 3

        node_payload = {
            "node_kind": "object",
            "name": concept_name,
            "description": "A party known across operating systems",
        }
        node_proposal = rvbbit.execute(
            """
            SELECT rvbbit.business_topology_propose(
                'node',%s::jsonb,0.94,'test',NULL,NULL,NULL,%s,NULL
            )
            """,
            (json.dumps(node_payload), node_proposal_key),
        ).fetchone()[0]
        proposal_ids.append(node_proposal)
        node_review = rvbbit.execute(
            "SELECT rvbbit.business_topology_review_proposal(%s,'accepted','test truth','pytest')",
            (node_proposal,),
        ).fetchone()[0]
        node_id = uuid.UUID(node_review["materialized"]["node_id"])

        population_payload = {
            "source_id": str(source_ids[0]),
            "population_kind": "composite",
            "display_name": f"Party identity bundle {suffix}",
            "selector": {"columns": ["party_pk", "email_address", "full_name"]},
        }
        population_proposal = rvbbit.execute(
            """
            SELECT rvbbit.business_topology_propose(
                'population',%s::jsonb,0.89,'test',NULL,NULL,NULL,%s,NULL
            )
            """,
            (json.dumps(population_payload), population_proposal_key),
        ).fetchone()[0]
        proposal_ids.append(population_proposal)
        population_review = rvbbit.execute(
            "SELECT rvbbit.business_topology_review_proposal(%s,'accepted','test motif','pytest')",
            (population_proposal,),
        ).fetchone()[0]
        inferred_population = uuid.UUID(
            population_review["materialized"]["population_id"]
        )
        inferred_kind, inferred_selector = rvbbit.execute(
            """
            SELECT population_kind,selector
              FROM rvbbit.business_topology_populations
             WHERE population_id=%s
            """,
            (inferred_population,),
        ).fetchone()
        assert inferred_kind == "composite"
        assert inferred_selector["columns"] == [
            "party_pk",
            "email_address",
            "full_name",
        ]

        binding_payload = {
            "node_id": str(node_id),
            "population_id": str(left_population),
            "binding_role": "identity",
            "authority": "primary",
        }
        binding_proposal = rvbbit.execute(
            """
            SELECT rvbbit.business_topology_propose(
                'binding',%s::jsonb,0.96,'test',NULL,NULL,NULL,%s,NULL
            )
            """,
            (json.dumps(binding_payload), binding_proposal_key),
        ).fetchone()[0]
        proposal_ids.append(binding_proposal)
        rvbbit.execute(
            "SELECT rvbbit.business_topology_review_proposal(%s,'accepted','test truth','pytest')",
            (binding_proposal,),
        )

        skeleton = rvbbit.execute(
            """
            SELECT population_count,source_count,bindings
              FROM rvbbit.business_topology_skeleton
             WHERE node_id=%s
            """,
            (node_id,),
        ).fetchone()
        assert skeleton[0] == 1
        assert skeleton[1] == 1
        assert skeleton[2][0]["authority"] == "primary"
        assert skeleton[2][0]["binding_role"] == "identity"
    finally:
        rvbbit.execute(
            "DELETE FROM rvbbit.business_topology_nodes WHERE name=%s",
            (concept_name,),
        )
        if proposal_ids:
            rvbbit.execute(
                "DELETE FROM rvbbit.business_topology_proposals WHERE proposal_id=ANY(%s)",
                (proposal_ids,),
            )
        if source_ids:
            rvbbit.execute(
                "DELETE FROM rvbbit.business_topology_sources WHERE source_id=ANY(%s)",
                (source_ids,),
            )
        rvbbit.execute(
            sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(
                sql.Identifier(left_table)
            )
        )
        rvbbit.execute(
            sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(
                sql.Identifier(right_table)
            )
        )


def test_business_topology_migration_is_registered():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    migration = (
        root / "crates/pg_rvbbit/sql/migrations/0279_business_topology_foundation.sql"
    )
    registry = (root / "crates/pg_rvbbit/src/migrations.rs").read_text(encoding="utf-8")
    migration_sql = migration.read_text(encoding="utf-8")

    assert '"0279_business_topology_foundation"' in registry
    assert (
        'include_str!("../sql/migrations/0279_business_topology_foundation.sql")'
        in registry
    )
    assert "ORDER BY random() LIMIT" not in migration_sql
    assert "rvbbit.business-topology.population.v1" in migration_sql
    assert "rvbbit.business-topology.correspondence.v1" in migration_sql


def test_business_topology_proposal_bundle_is_reviewable_but_not_promotable(rvbbit):
    suffix = uuid.uuid4().hex
    work_id = f"work:test-{suffix}"
    plan_sha256 = suffix + suffix
    result = {
        "schema_version": "rvbbit.business-topology.neighborhood-skeleton-result.v1",
        "work_id": work_id,
        "status": "proposed",
        "canonical_name": "Test Business Area",
        "nodes": [],
        "bindings": [],
        "edges": [],
        "unbound_population_ids": [],
        "rationale": "A deliberately empty test skeleton.",
    }
    receipt = {
        "schema_version": "rvbbit.business-topology.work-receipt.v1",
        "worker_version": "pytest-worker-v1",
        "prompt_contract_version": "pytest-prompts-v1",
        "plan_sha256": plan_sha256,
        "work_id": work_id,
        "work_kind": "neighborhood_synthesis",
        "input_packet_sha256": "b" * 64,
        "completed_at": "2026-08-11T12:00:00+00:00",
        "result": result,
        "validation": {
            "valid": True,
            "work_id": work_id,
            "work_kind": "neighborhood_synthesis",
            "status": "proposed",
            "nodes": 0,
            "bindings": 0,
            "edges": 0,
            "unbound_populations": 0,
        },
        "execution_receipts": [
            {
                "model_version": "pytest-model-v1",
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "cost": 0.001,
                },
            }
        ],
    }
    context = {
        "schema_version": "rvbbit.business-topology.bundle-context.v1",
        "sources": [{"source_key": f"source:{suffix}", "name": "test"}],
        "populations": [],
        "privacy": {"raw_values": False, "value_hashes": False},
    }
    before = rvbbit.execute(
        """
        SELECT (SELECT count(*) FROM rvbbit.business_topology_nodes),
               (SELECT count(*) FROM rvbbit.business_topology_bindings),
               (SELECT count(*) FROM rvbbit.business_topology_edges)
        """
    ).fetchone()
    bundle_id = None
    bridge_bundle_id = None
    try:
        bundle_id = rvbbit.execute(
            """
            SELECT rvbbit.business_topology_stage_proposal_bundle(
                %s::jsonb,'excavation_unit',%s,%s::text[],%s::jsonb,'pytest',NULL
            )
            """,
            (
                json.dumps(receipt),
                f"excavation:{suffix}",
                [f"source:{suffix}"],
                json.dumps(context),
            ),
        ).fetchone()[0]
        summary = rvbbit.execute(
            """
            SELECT status,source_count,node_count,binding_count,edge_count,
                   total_tokens,cost,model_versions
              FROM rvbbit.business_topology_proposal_bundle_summary
             WHERE bundle_id=%s
            """,
            (bundle_id,),
        ).fetchone()
        assert summary[:5] == ("proposed", 1, 0, 0, 0)
        assert summary[5] == 15
        assert float(summary[6]) == 0.001
        assert summary[7] == ["pytest-model-v1"]

        correction = {
            "schema_version": "rvbbit.business-topology.bundle-correction.v1",
            "canonical_name": "Corrected Test Area",
            "node_patches": [],
            "binding_patches": [],
            "relationship_suggestions": [],
            "review_note": "A reviewer-composed overlay, not a promoted topology.",
        }
        saved_correction = rvbbit.execute(
            """
            SELECT rvbbit.business_topology_save_bundle_correction(
                %s,%s::jsonb,'draft','pytest',0
            )
            """,
            (bundle_id, json.dumps(correction)),
        ).fetchone()[0]
        assert saved_correction["revision"] == 1
        assert saved_correction["materialized_topology"] is False
        corrected = rvbbit.execute(
            """
            SELECT correction_revision,correction_state,
                   correction->>'canonical_name',result->>'canonical_name'
              FROM rvbbit.business_topology_proposal_bundle_review
             WHERE bundle_id=%s
            """,
            (bundle_id,),
        ).fetchone()
        assert corrected == (
            1,
            "draft",
            "Corrected Test Area",
            "Test Business Area",
        )
        with pytest.raises(Exception, match="changed from expected revision"):
            rvbbit.execute(
                """
                SELECT rvbbit.business_topology_save_bundle_correction(
                    %s,%s::jsonb,'draft','pytest',0
                )
                """,
                (bundle_id, json.dumps(correction)),
            )
        invalid_correction = json.loads(json.dumps(correction))
        invalid_correction["node_patches"] = [
            {"node_key": "outside-receipt", "name": "Invented node"}
        ]
        with pytest.raises(Exception, match="outside the immutable receipt"):
            rvbbit.execute(
                """
                SELECT rvbbit.business_topology_save_bundle_correction(
                    %s,%s::jsonb,'draft','pytest',1
                )
                """,
                (bundle_id, json.dumps(invalid_correction)),
            )
        completed_correction = rvbbit.execute(
            """
            SELECT rvbbit.business_topology_save_bundle_correction(
                %s,%s::jsonb,'complete','pytest',1
            )
            """,
            (bundle_id, json.dumps(correction)),
        ).fetchone()[0]
        assert completed_correction["revision"] == 2
        assert completed_correction["correction_state"] == "complete"

        bridge_work_id = f"work:bridge-{suffix}"
        bridge_receipt = json.loads(json.dumps(receipt))
        bridge_receipt.update(
            {
                "work_id": bridge_work_id,
                "work_kind": "bridge_synthesis",
                "result": {
                    "schema_version": "rvbbit.business-topology.bridge-result.v1",
                    "work_id": bridge_work_id,
                    "status": "proposed",
                    "merge_excavation_units": False,
                    "findings": [
                        {
                            "finding_key": "bounded-test",
                            "outcome": "unrelated",
                            "confidence": 0.61,
                            "evidence_work_ids": [],
                        }
                    ],
                },
                "validation": {
                    "valid": True,
                    "work_id": bridge_work_id,
                    "work_kind": "bridge_synthesis",
                    "status": "proposed",
                    "findings": 1,
                },
            }
        )
        bridge_bundle_id = rvbbit.execute(
            """
            SELECT rvbbit.business_topology_stage_proposal_bundle(
                %s::jsonb,'boundary_link',%s,%s::text[],%s::jsonb,'pytest',NULL
            )
            """,
            (
                json.dumps(bridge_receipt),
                f"link:{suffix}",
                [f"source:{suffix}", f"source:other-{suffix}"],
                json.dumps(context),
            ),
        ).fetchone()[0]
        bridge_summary = rvbbit.execute(
            """
            SELECT edge_count,finding_count
              FROM rvbbit.business_topology_proposal_bundle_summary
             WHERE bundle_id=%s
            """,
            (bridge_bundle_id,),
        ).fetchone()
        assert bridge_summary == (0, 1)

        with pytest.raises(Exception, match="acceptance is not implemented"):
            rvbbit.execute(
                "SELECT rvbbit.business_topology_review_proposal_bundle(%s,'accepted')",
                (bundle_id,),
            )
        review = rvbbit.execute(
            """
            SELECT rvbbit.business_topology_review_proposal_bundle(
                %s,'needs_revision','test correction','pytest'
            )
            """,
            (bundle_id,),
        ).fetchone()[0]
        assert review["status"] == "needs_revision"
        assert review["materialized_topology"] is False

        restaged = rvbbit.execute(
            """
            SELECT rvbbit.business_topology_stage_proposal_bundle(
                %s::jsonb,'excavation_unit',%s,%s::text[],%s::jsonb,'pytest',NULL
            )
            """,
            (
                json.dumps(receipt),
                f"excavation:{suffix}",
                [f"source:{suffix}"],
                json.dumps(context),
            ),
        ).fetchone()[0]
        assert restaged == bundle_id

        bad_receipt = json.loads(json.dumps(receipt))
        bad_receipt["result"]["nodes"] = [{"sql": "select secret"}]
        with pytest.raises(Exception, match="forbidden"):
            rvbbit.execute(
                """
                SELECT rvbbit.business_topology_stage_proposal_bundle(
                    %s::jsonb,'excavation_unit',%s,%s::text[],%s::jsonb,'pytest',NULL
                )
                """,
                (
                    json.dumps(bad_receipt),
                    f"excavation:{suffix}",
                    [f"source:{suffix}"],
                    json.dumps(context),
                ),
            )

        rvbbit.execute(
            """
            SELECT rvbbit.business_topology_review_proposal_bundle(
                %s,'rejected','test rejection','pytest'
            )
            """,
            (bundle_id,),
        )
        with pytest.raises(Exception, match="cannot be overwritten"):
            rvbbit.execute(
                """
                SELECT rvbbit.business_topology_stage_proposal_bundle(
                    %s::jsonb,'excavation_unit',%s,%s::text[],%s::jsonb,'pytest',NULL
                )
                """,
                (
                    json.dumps(receipt),
                    f"excavation:{suffix}",
                    [f"source:{suffix}"],
                    json.dumps(context),
                ),
            )
        after = rvbbit.execute(
            """
            SELECT (SELECT count(*) FROM rvbbit.business_topology_nodes),
                   (SELECT count(*) FROM rvbbit.business_topology_bindings),
                   (SELECT count(*) FROM rvbbit.business_topology_edges)
            """
        ).fetchone()
        assert after == before
    finally:
        if bundle_id is not None:
            rvbbit.execute(
                "DELETE FROM rvbbit.business_topology_bundle_corrections WHERE bundle_id=%s",
                (bundle_id,),
            )
        if bridge_bundle_id is not None:
            rvbbit.execute(
                "DELETE FROM rvbbit.business_topology_proposal_bundles WHERE bundle_id=%s",
                (bridge_bundle_id,),
            )
        if bundle_id is not None:
            rvbbit.execute(
                "DELETE FROM rvbbit.business_topology_proposal_bundles WHERE bundle_id=%s",
                (bundle_id,),
            )


def test_business_topology_proposal_bundle_migration_is_registered():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    migration = (
        root
        / "crates/pg_rvbbit/sql/migrations/0282_business_topology_proposal_bundles.sql"
    )
    registry = (root / "crates/pg_rvbbit/src/migrations.rs").read_text(encoding="utf-8")
    migration_sql = migration.read_text(encoding="utf-8")

    assert '"0282_business_topology_proposal_bundles"' in registry
    assert (
        'include_str!("../sql/migrations/0282_business_topology_proposal_bundles.sql")'
        in registry
    )
    assert "cannot be accepted or materialized" in migration_sql
    assert "v_result->'findings'" in migration_sql
    assert "finding_count" in migration_sql

    correction_migration = (
        root
        / "crates/pg_rvbbit/sql/migrations/0284_business_topology_bundle_corrections.sql"
    )
    correction_sql = correction_migration.read_text(encoding="utf-8")
    assert '"0284_business_topology_bundle_corrections"' in registry
    assert (
        'include_str!("../sql/migrations/0284_business_topology_bundle_corrections.sql")'
        in registry
    )
    assert "business_topology_save_bundle_correction" in correction_sql
    assert "materialized_topology',false" in correction_sql
