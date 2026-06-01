"""Sanity checks: extension loaded, catalog populated."""

import json
import uuid


def test_extension_loaded(rvbbit):
    row = rvbbit.execute("SELECT rvbbit.rvbbit_version()").fetchone()
    assert row is not None
    # Loose semver match so test doesn't break on every version bump.
    parts = row[0].split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts), row[0]


def test_rvbbit_schema_present(rvbbit):
    row = rvbbit.execute(
        "SELECT count(*) FROM pg_namespace WHERE nspname = 'rvbbit'"
    ).fetchone()
    assert row[0] == 1


def test_catalog_tables_present(rvbbit):
    expected = {
        "tables",
        "row_groups",
        "delete_log",
        "shreds",
        "operators",
        "receipts",
        "capability_catalog",
    }
    rows = rvbbit.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'rvbbit'"
    ).fetchall()
    present = {r[0] for r in rows}
    missing = expected - present
    assert not missing, f"missing catalog tables: {missing}"


def test_capability_catalog_seeded(rvbbit):
    row = rvbbit.execute(
        """
        SELECT
          count(*) FILTER (WHERE active) AS active_entries,
          count(*) FILTER (WHERE active AND kind = 'runtime_sidecar') AS runtime_entries,
          count(*) FILTER (WHERE active AND id = 'runtimes/python-runtime') AS python_runtime_entries,
          count(*) FILTER (
            WHERE active
              AND id = 'smoke/warren-echo'
              AND catalog_entry->'acceptance_tests' ? 'echo_operator_sample_table'
          ) AS acceptance_entries,
          count(*) FILTER (
            WHERE active
              AND id = 'rerank/bge-reranker-v2-m3'
              AND operators @> ARRAY['about','means','semantic_score']::text[]
          ) AS bundled_operator_entries
        FROM rvbbit.capability_catalog
        """
    ).fetchone()
    assert row[0] >= 1
    assert row[1] >= 1
    assert row[2] == 1
    assert row[3] == 1
    assert row[4] == 1


def test_access_method_registered(rvbbit):
    row = rvbbit.execute(
        "SELECT count(*) FROM pg_am WHERE amname = 'rvbbit'"
    ).fetchone()
    assert row[0] == 1


def test_warren_job_progress_contract(rvbbit):
    node_name = f"test-warren-{uuid.uuid4().hex[:8]}"
    job_name = f"test-capability-{uuid.uuid4().hex[:8]}"
    job_id = None
    replacement_job_id = None
    try:
        rvbbit.execute(
            "SELECT rvbbit.register_warren_node(%s, NULL, '{}'::jsonb, '{}'::jsonb, 'test')",
            (node_name,),
        )
        job_id = rvbbit.execute(
            """
            SELECT rvbbit.enqueue_warren_job(
              'capability',
              %s,
              '{"name":"test-capability"}'::jsonb,
              '{}'::jsonb,
              'running'
            )
            """,
            (job_name,),
        ).fetchone()[0]

        row = rvbbit.execute(
            "SELECT status, phase, progress FROM rvbbit.warren_jobs WHERE job_id = %s",
            (job_id,),
        ).fetchone()
        assert row[0] == "queued"
        assert row[1] == "queued"
        assert row[2] == {}

        claimed = rvbbit.execute(
            "SELECT job_id FROM rvbbit.claim_warren_job(%s)",
            (node_name,),
        ).fetchone()
        assert claimed[0] == job_id

        rvbbit.execute(
            """
            SELECT rvbbit.update_warren_job_progress(
              %s,
              %s,
              'starting',
              '{"port": 8123, "container_name": "rvbbit-test"}'::jsonb
            )
            """,
            (job_id, node_name),
        )
        row = rvbbit.execute(
            "SELECT status, phase, progress FROM rvbbit.warren_jobs WHERE job_id = %s",
            (job_id,),
        ).fetchone()
        assert row[0] == "running"
        assert row[1] == "starting"
        assert row[2]["phase"] == "starting"
        assert row[2]["port"] == 8123

        rvbbit.execute(
            """
            SELECT rvbbit.complete_warren_job(
              job_id => %s,
              node_name => %s,
              endpoint_url => 'http://rvbbit-test:8080/predict',
              deploy_manifest => '{"name":"test-capability"}'::jsonb,
              health => '{"ok": true}'::jsonb,
              logs => '{"agent":"test"}'::jsonb
            )
            """,
            (job_id, node_name),
        )
        row = rvbbit.execute(
            "SELECT status, phase, endpoint_url, progress FROM rvbbit.warren_jobs WHERE job_id = %s",
            (job_id,),
        ).fetchone()
        assert row[0] == "completed"
        assert row[1] == "ready"
        assert row[2] == "http://rvbbit-test:8080/predict"
        assert row[3]["phase"] == "ready"

        replacement_job_id = rvbbit.execute(
            """
            SELECT rvbbit.enqueue_warren_job(
              'capability',
              %s,
              '{"name":"test-capability","version":2}'::jsonb,
              '{}'::jsonb,
              'running'
            )
            """,
            (job_name,),
        ).fetchone()[0]
        claimed = rvbbit.execute(
            "SELECT job_id FROM rvbbit.claim_warren_job(%s)",
            (node_name,),
        ).fetchone()
        assert claimed[0] == replacement_job_id
        rvbbit.execute(
            """
            SELECT rvbbit.complete_warren_job(
              job_id => %s,
              node_name => %s,
              endpoint_url => 'http://rvbbit-test:8080/v2',
              deploy_manifest => '{"name":"test-capability","version":2}'::jsonb,
              health => '{"ok": true}'::jsonb,
              logs => '{"agent":"test"}'::jsonb
            )
            """,
            (replacement_job_id, node_name),
        )
        active_deployment = rvbbit.execute(
            """
            SELECT count(*), max(job_id::text), max(endpoint_url)
            FROM rvbbit.warren_deployments
            WHERE node_name = %s
              AND kind = 'capability'
              AND name = %s
              AND status IN ('starting', 'running')
            """,
            (node_name, job_name),
        ).fetchone()
        assert active_deployment == (
            1,
            str(replacement_job_id),
            "http://rvbbit-test:8080/v2",
        )
    finally:
        if replacement_job_id is not None:
            rvbbit.execute(
                "DELETE FROM rvbbit.warren_deployments WHERE job_id = %s",
                (replacement_job_id,),
            )
            rvbbit.execute(
                "DELETE FROM rvbbit.warren_jobs WHERE job_id = %s",
                (replacement_job_id,),
            )
        if job_id is not None:
            rvbbit.execute("DELETE FROM rvbbit.warren_deployments WHERE job_id = %s", (job_id,))
            rvbbit.execute("DELETE FROM rvbbit.warren_jobs WHERE job_id = %s", (job_id,))
        rvbbit.execute("DELETE FROM rvbbit.warren_nodes WHERE name = %s", (node_name,))


def test_warren_deployment_stop_lifecycle_and_drift(rvbbit):
    node_name = f"test-warren-life-{uuid.uuid4().hex[:8]}"
    other_node_name = f"test-warren-life-other-{uuid.uuid4().hex[:8]}"
    job_name = f"test-runtime-{uuid.uuid4().hex[:8]}"
    runtime_name = f"test_runtime_{uuid.uuid4().hex[:8]}"
    deploy_job_id = None
    stop_job_id = None
    deployment_id = None
    try:
        for name in [node_name, other_node_name]:
            rvbbit.execute(
                "SELECT rvbbit.register_warren_node(%s, NULL, '{}'::jsonb, '{}'::jsonb, 'test')",
                (name,),
            )
        deploy_job_id = rvbbit.execute(
            """
            SELECT rvbbit.enqueue_warren_job(
              'capability',
              %s,
              '{"name":"test-runtime"}'::jsonb,
              '{}'::jsonb,
              'running'
            )
            """,
            (job_name,),
        ).fetchone()[0]
        claimed = rvbbit.execute(
            "SELECT job_id FROM rvbbit.claim_warren_job(%s)",
            (node_name,),
        ).fetchone()
        assert claimed[0] == deploy_job_id
        rvbbit.execute(
            """
            SELECT rvbbit.complete_warren_job(
              job_id => %s,
              node_name => %s,
              endpoint_url => 'http://rvbbit-test-runtime:8080/run',
              deploy_manifest => '{"name":"test-runtime"}'::jsonb,
              compose_project => 'test_runtime_project',
              work_dir => '/tmp/rvbbit-test-runtime',
              health => '{"ok": true}'::jsonb,
              runtime_name => %s
            )
            """,
            (deploy_job_id, node_name, runtime_name),
        )
        deployment_id = rvbbit.execute(
            """
            SELECT deployment_id
            FROM rvbbit.warren_deployments
            WHERE job_id = %s
            """,
            (deploy_job_id,),
        ).fetchone()[0]
        rvbbit.execute(
            """
            SELECT rvbbit.register_python_runtime(
              runtime_name => %s,
              endpoint_url => 'http://rvbbit-test-runtime:8080/run',
              runtime_source => 'warren',
              set_default => false
            )
            """,
            (runtime_name,),
        )

        stop_job_id = rvbbit.execute(
            "SELECT rvbbit.request_warren_deployment_stop(%s)",
            (deployment_id,),
        ).fetchone()[0]
        queued = rvbbit.execute(
            """
            SELECT desired_state, status, progress->>'deployment_id'
            FROM rvbbit.warren_jobs
            WHERE job_id = %s
            """,
            (stop_job_id,),
        ).fetchone()
        assert queued == ("stopped", "queued", str(deployment_id))
        stopping = rvbbit.execute(
            "SELECT status FROM rvbbit.warren_deployments WHERE deployment_id = %s",
            (deployment_id,),
        ).fetchone()
        assert stopping[0] == "stopping"

        assert (
            rvbbit.execute(
                "SELECT job_id FROM rvbbit.claim_warren_job(%s)",
                (other_node_name,),
            ).fetchone()
            is None
        )
        claimed = rvbbit.execute(
            """
            SELECT job_id, desired_state, manifest #>> '{warren_deployment,node_name}'
            FROM rvbbit.claim_warren_job(%s)
            """,
            (node_name,),
        ).fetchone()
        assert claimed == (stop_job_id, "stopped", node_name)
        rvbbit.execute(
            """
            SELECT rvbbit.complete_warren_job(
              job_id => %s,
              node_name => %s,
              deployment_status => 'stopped',
              endpoint_url => 'http://rvbbit-test-runtime:8080/run',
              deploy_manifest => '{"name":"test-runtime"}'::jsonb,
              compose_project => 'test_runtime_project',
              work_dir => '/tmp/rvbbit-test-runtime',
              health => '{"ok": true, "action":"stopped"}'::jsonb,
              runtime_name => %s
            )
            """,
            (stop_job_id, node_name, runtime_name),
        )
        stopped = rvbbit.execute(
            """
            SELECT d.status, j.phase, r.status
            FROM rvbbit.warren_deployments d
            JOIN rvbbit.warren_jobs j ON j.job_id = %s
            LEFT JOIN rvbbit.python_runtimes r ON r.name = %s
            WHERE d.deployment_id = %s
            """,
            (stop_job_id, runtime_name, deployment_id),
        ).fetchone()
        assert stopped == ("stopped", "stopped", "disabled")

        drifted = rvbbit.execute(
            """
            SELECT rvbbit.report_warren_deployment_observation(
              %s, %s, 'running', '{"container":"rvbbit-test-runtime"}'::jsonb
            )
            """,
            (deployment_id, node_name),
        ).fetchone()
        assert drifted[0] == "orphaned"
        recovered = rvbbit.execute(
            """
            SELECT rvbbit.report_warren_deployment_observation(
              %s, %s, 'missing', '{"container":"rvbbit-test-runtime"}'::jsonb
            )
            """,
            (deployment_id, node_name),
        ).fetchone()
        assert recovered[0] == "stopped"
    finally:
        rvbbit.execute("DELETE FROM rvbbit.python_runtimes WHERE name = %s", (runtime_name,))
        if deployment_id is not None:
            rvbbit.execute(
                "DELETE FROM rvbbit.warren_deployments WHERE deployment_id = %s",
                (deployment_id,),
            )
        for cleanup_job_id in [stop_job_id, deploy_job_id]:
            if cleanup_job_id is not None:
                rvbbit.execute(
                    "DELETE FROM rvbbit.warren_jobs WHERE job_id = %s",
                    (cleanup_job_id,),
                )
        rvbbit.execute(
            "DELETE FROM rvbbit.warren_nodes WHERE name IN (%s, %s)",
            (node_name, other_node_name),
        )


def test_warren_effective_status_backend_callable_and_redeploy(rvbbit):
    node_name = f"test-warren-state-{uuid.uuid4().hex[:8]}"
    job_name = f"test-backend-{uuid.uuid4().hex[:8]}"
    backend_name = f"test_backend_{uuid.uuid4().hex[:8]}"
    deploy_job_id = None
    stop_job_id = None
    redeploy_job_id = None
    deployment_id = None
    manifest = {
        "name": job_name,
        "backend": {
            "name": backend_name,
            "transport": "stub",
        },
    }
    try:
        rvbbit.execute(
            "SELECT rvbbit.register_warren_node(%s, NULL, '{}'::jsonb, '{}'::jsonb, 'test')",
            (node_name,),
        )
        state = rvbbit.execute(
            """
            SELECT effective_status, heartbeat_state, is_eligible
            FROM rvbbit.warren_node_effective_status
            WHERE name = %s
            """,
            (node_name,),
        ).fetchone()
        assert state == ("ready", "fresh", True)

        deploy_job_id = rvbbit.execute(
            """
            SELECT rvbbit.enqueue_warren_job(
              'capability',
              %s,
              %s::jsonb,
              '{}'::jsonb,
              'running'
            )
            """,
            (job_name, json.dumps(manifest)),
        ).fetchone()[0]
        claimed = rvbbit.execute(
            "SELECT job_id FROM rvbbit.claim_warren_job(%s)",
            (node_name,),
        ).fetchone()
        assert claimed[0] == deploy_job_id
        rvbbit.execute(
            """
            SELECT rvbbit.complete_warren_job(
              job_id => %s,
              node_name => %s,
              endpoint_url => 'http://rvbbit-test-backend:8080/predict',
              backend_name => %s,
              deploy_manifest => %s::jsonb,
              health => '{"ok": true}'::jsonb,
              logs => '{"agent":"test"}'::jsonb
            )
            """,
            (deploy_job_id, node_name, backend_name, json.dumps(manifest)),
        )
        deployment_id = rvbbit.execute(
            """
            SELECT deployment_id
            FROM rvbbit.warren_deployments
            WHERE job_id = %s
            """,
            (deploy_job_id,),
        ).fetchone()[0]
        rvbbit.execute(
            """
            SELECT rvbbit.register_backend(
              backend_name => %s,
              backend_endpoint => 'http://rvbbit-test-backend:8080/predict',
              backend_transport => 'stub',
              backend_install_manifest => %s::jsonb
            )
            """,
            (backend_name, json.dumps(manifest)),
        )
        rvbbit.execute("SELECT rvbbit.reload_backends()")
        callable_state = rvbbit.execute(
            """
            SELECT deployment_status, serving_status, callable
            FROM rvbbit.warren_backend_status
            WHERE name = %s
            """,
            (backend_name,),
        ).fetchone()
        assert callable_state == ("running", "running", True)

        stop_job_id = rvbbit.execute(
            "SELECT rvbbit.request_warren_deployment_stop(%s)",
            (deployment_id,),
        ).fetchone()[0]
        claimed = rvbbit.execute(
            "SELECT job_id FROM rvbbit.claim_warren_job(%s)",
            (node_name,),
        ).fetchone()
        assert claimed[0] == stop_job_id
        rvbbit.execute(
            """
            SELECT rvbbit.complete_warren_job(
              job_id => %s,
              node_name => %s,
              deployment_status => 'stopped',
              endpoint_url => 'http://rvbbit-test-backend:8080/predict',
              backend_name => %s,
              deploy_manifest => %s::jsonb,
              health => '{"ok": true, "action":"stopped"}'::jsonb,
              logs => '{"agent":"test"}'::jsonb
            )
            """,
            (stop_job_id, node_name, backend_name, json.dumps(manifest)),
        )
        stopped_state = rvbbit.execute(
            """
            SELECT deployment_status, serving_status, callable
            FROM rvbbit.warren_backend_status
            WHERE name = %s
            """,
            (backend_name,),
        ).fetchone()
        assert stopped_state == ("stopped", "unavailable", False)
        probe = rvbbit.execute(
            "SELECT rvbbit.backend_probe(%s)",
            (backend_name,),
        ).fetchone()[0]
        assert probe["ok"] is False
        assert "not callable" in probe["error"]

        redeploy_job_id = rvbbit.execute(
            "SELECT rvbbit.request_warren_deployment_redeploy(%s)",
            (deployment_id,),
        ).fetchone()[0]
        redeploy = rvbbit.execute(
            """
            SELECT desired_state, status, progress->>'redeploy_of'
            FROM rvbbit.warren_jobs
            WHERE job_id = %s
            """,
            (redeploy_job_id,),
        ).fetchone()
        assert redeploy == ("running", "queued", str(deployment_id))

        rvbbit.execute(
            """
            UPDATE rvbbit.warren_nodes
            SET last_heartbeat = clock_timestamp() - interval '3 minutes'
            WHERE name = %s
            """,
            (node_name,),
        )
        offline_state = rvbbit.execute(
            """
            SELECT effective_status, heartbeat_state, is_eligible
            FROM rvbbit.warren_node_effective_status
            WHERE name = %s
            """,
            (node_name,),
        ).fetchone()
        assert offline_state == ("offline", "offline", False)
        assert (
            rvbbit.execute(
                "SELECT job_id FROM rvbbit.claim_warren_job(%s)",
                (node_name,),
            ).fetchone()
            is None
        )
    finally:
        rvbbit.execute("DELETE FROM rvbbit.backends WHERE name = %s", (backend_name,))
        if deployment_id is not None:
            rvbbit.execute(
                "DELETE FROM rvbbit.warren_deployments WHERE deployment_id = %s",
                (deployment_id,),
            )
        for cleanup_job_id in [redeploy_job_id, stop_job_id, deploy_job_id]:
            if cleanup_job_id is not None:
                rvbbit.execute(
                    "DELETE FROM rvbbit.warren_jobs WHERE job_id = %s",
                    (cleanup_job_id,),
                )
        rvbbit.execute("DELETE FROM rvbbit.warren_nodes WHERE name = %s", (node_name,))
        rvbbit.execute("SELECT rvbbit.reload_backends()")


def test_warren_gpu_capacity_gates_claims(rvbbit):
    node_name = f"test-gpu-warren-{uuid.uuid4().hex[:8]}"
    jobs: list[str] = []
    gib = 1024 * 1024 * 1024
    try:
        rvbbit.execute(
            """
            SELECT rvbbit.register_warren_node(
              %s,
              NULL,
              '{"capability":true,"docker":true,"gpu":true}'::jsonb,
              '{"gpu":{"vram_usable_ratio":0.9}}'::jsonb,
              'test'
            )
            """,
            (node_name,),
        )
        metrics = {
            "summary": {
                "gpu_count": 1,
                "gpu_mem_used_bytes": 0,
                "gpu_mem_total_bytes": 10 * gib,
            },
            "gpus": [
                {
                    "index": 0,
                    "name": "Test GPU",
                    "uuid": "GPU-test",
                    "memory_used_bytes": 0,
                    "memory_total_bytes": 10 * gib,
                }
            ],
        }
        rvbbit.execute(
            "SELECT rvbbit.record_warren_metrics(%s, %s::jsonb)",
            (node_name, json.dumps(metrics)),
        )
        helper_row = rvbbit.execute(
            """
            SELECT
              rvbbit.capability_gpu_required(%s::jsonb),
              rvbbit.capability_vram_required_bytes(%s::jsonb),
              rvbbit.capability_gpu_reserved(%s::jsonb)
            """,
            (
                '{"resources":{"gpu":{"required":true,"vram_required_bytes":123}}}',
                '{"resources":{"gpu":{"vram_required_bytes":123}}}',
                '{"resources":{"gpu":{"reserved":true,"vram_required_bytes":123}}}',
            ),
        ).fetchone()
        assert helper_row == (True, 123, True)

        first_manifest = {
            "name": "gpu-first",
            "resources": {
                "gpu": {
                    "required": False,
                    "placement": "single_gpu",
                    "vram_required_bytes": 6 * gib,
                }
            },
        }
        first_job = rvbbit.execute(
            """
            SELECT rvbbit.enqueue_warren_job(
              'capability',
              'gpu-first',
              %s::jsonb,
              '{"gpu":true}'::jsonb,
              'running'
            )
            """,
            (json.dumps(first_manifest),),
        ).fetchone()[0]
        jobs.append(first_job)
        claimed = rvbbit.execute(
            "SELECT job_id FROM rvbbit.claim_warren_job(%s)",
            (node_name,),
        ).fetchone()
        assert claimed[0] == first_job
        stored_manifest = rvbbit.execute(
            "SELECT manifest FROM rvbbit.warren_jobs WHERE job_id = %s",
            (first_job,),
        ).fetchone()[0]
        assert stored_manifest["resources"]["gpu"]["reserved"] is True
        rvbbit.execute(
            """
            SELECT rvbbit.complete_warren_job(
              job_id => %s,
              node_name => %s,
              endpoint_url => 'http://gpu-first:8080/predict',
              deploy_manifest => %s::jsonb,
              health => '{"ok": true}'::jsonb
            )
            """,
            (first_job, node_name, json.dumps(stored_manifest)),
        )

        capacity = rvbbit.execute(
            """
            SELECT gpu_provisioned_bytes, gpu_available_bytes, gpu_names
            FROM rvbbit.warren_gpu_capacity
            WHERE node_name = %s
            """,
            (node_name,),
        ).fetchone()
        assert capacity[0] == 6 * gib
        assert capacity[1] == 3 * gib
        assert "Test GPU" in capacity[2]

        too_large = {
            "name": "gpu-too-large",
            "resources": {"gpu": {"vram_required_bytes": 4 * gib}},
        }
        too_large_job = rvbbit.execute(
            """
            SELECT rvbbit.enqueue_warren_job(
              'capability',
              'gpu-too-large',
              %s::jsonb,
              '{"gpu":true}'::jsonb,
              'running'
            )
            """,
            (json.dumps(too_large),),
        ).fetchone()[0]
        jobs.append(too_large_job)

        small = {
            "name": "gpu-small",
            "resources": {"gpu": {"vram_required_bytes": 2 * gib}},
        }
        small_job = rvbbit.execute(
            """
            SELECT rvbbit.enqueue_warren_job(
              'capability',
              'gpu-small',
              %s::jsonb,
              '{"gpu":true}'::jsonb,
              'running'
            )
            """,
            (json.dumps(small),),
        ).fetchone()[0]
        jobs.append(small_job)

        claimed = rvbbit.execute(
            "SELECT job_id FROM rvbbit.claim_warren_job(%s)",
            (node_name,),
        ).fetchone()
        assert claimed[0] == small_job
        blocked = rvbbit.execute(
            "SELECT status FROM rvbbit.warren_jobs WHERE job_id = %s",
            (too_large_job,),
        ).fetchone()
        assert blocked[0] == "queued"
    finally:
        for cleanup_job_id in jobs:
            rvbbit.execute(
                "DELETE FROM rvbbit.warren_deployments WHERE job_id = %s",
                (cleanup_job_id,),
            )
            rvbbit.execute(
                "DELETE FROM rvbbit.warren_jobs WHERE job_id = %s",
                (cleanup_job_id,),
            )
        rvbbit.execute("DELETE FROM rvbbit.warren_nodes WHERE name = %s", (node_name,))


def test_warren_gpu_single_gpu_placement_is_not_aggregate(rvbbit):
    node_name = f"test-dual-gpu-warren-{uuid.uuid4().hex[:8]}"
    jobs: list[str] = []
    gib = 1024 * 1024 * 1024
    try:
        rvbbit.execute(
            """
            SELECT rvbbit.register_warren_node(
              %s,
              NULL,
              '{"capability":true,"docker":true,"gpu":true}'::jsonb,
              '{"gpu":{"vram_usable_ratio":0.9}}'::jsonb,
              'test'
            )
            """,
            (node_name,),
        )
        metrics = {
            "summary": {
                "gpu_count": 2,
                "gpu_mem_used_bytes": 0,
                "gpu_mem_total_bytes": 8 * gib,
            },
            "gpus": [
                {
                    "index": 0,
                    "name": "Test GPU 0",
                    "uuid": "GPU-test-0",
                    "memory_used_bytes": 0,
                    "memory_total_bytes": 4 * gib,
                },
                {
                    "index": 1,
                    "name": "Test GPU 1",
                    "uuid": "GPU-test-1",
                    "memory_used_bytes": 0,
                    "memory_total_bytes": 4 * gib,
                },
            ],
        }
        rvbbit.execute(
            "SELECT rvbbit.record_warren_metrics(%s, %s::jsonb)",
            (node_name, json.dumps(metrics)),
        )
        capacity = rvbbit.execute(
            """
            SELECT gpu_mem_usable_bytes, single_gpu_mem_usable_bytes, gpu_available_bytes
            FROM rvbbit.warren_gpu_capacity
            WHERE node_name = %s
            """,
            (node_name,),
        ).fetchone()
        assert capacity == (
            (8 * gib * 9) // 10,
            (4 * gib * 9) // 10,
            (8 * gib * 9) // 10,
        )

        too_large = {
            "name": "gpu-too-large-for-one-card",
            "resources": {
                "gpu": {
                    "placement": "single_gpu",
                    "vram_required_bytes": 6 * gib,
                }
            },
        }
        too_large_job = rvbbit.execute(
            """
            SELECT rvbbit.enqueue_warren_job(
              'capability',
              'gpu-too-large-for-one-card',
              %s::jsonb,
              '{"gpu":true}'::jsonb,
              'running'
            )
            """,
            (json.dumps(too_large),),
        ).fetchone()[0]
        jobs.append(too_large_job)

        assert (
            rvbbit.execute(
                "SELECT job_id FROM rvbbit.claim_warren_job(%s)",
                (node_name,),
            ).fetchone()
            is None
        )

        small = {
            "name": "gpu-fits-one-card",
            "resources": {
                "gpu": {
                    "placement": "single_gpu",
                    "vram_required_bytes": 3 * gib,
                }
            },
        }
        small_job = rvbbit.execute(
            """
            SELECT rvbbit.enqueue_warren_job(
              'capability',
              'gpu-fits-one-card',
              %s::jsonb,
              '{"gpu":true}'::jsonb,
              'running'
            )
            """,
            (json.dumps(small),),
        ).fetchone()[0]
        jobs.append(small_job)

        claimed = rvbbit.execute(
            "SELECT job_id FROM rvbbit.claim_warren_job(%s)",
            (node_name,),
        ).fetchone()
        assert claimed[0] == small_job
    finally:
        for cleanup_job_id in jobs:
            rvbbit.execute(
                "DELETE FROM rvbbit.warren_deployments WHERE job_id = %s",
                (cleanup_job_id,),
            )
            rvbbit.execute(
                "DELETE FROM rvbbit.warren_jobs WHERE job_id = %s",
                (cleanup_job_id,),
            )
        rvbbit.execute("DELETE FROM rvbbit.warren_nodes WHERE name = %s", (node_name,))


def test_capability_catalog_resource_profile_seeded(rvbbit):
    row = rvbbit.execute(
        """
        SELECT count(*)
        FROM rvbbit.capability_catalog
        WHERE vram_required_bytes IS NOT NULL
          AND resource_profile ? 'gpu'
        """
    ).fetchone()
    assert row[0] >= 10
