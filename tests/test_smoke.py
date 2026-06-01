"""Sanity checks: extension loaded, catalog populated."""

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
    finally:
        if job_id is not None:
            rvbbit.execute("DELETE FROM rvbbit.warren_deployments WHERE job_id = %s", (job_id,))
            rvbbit.execute("DELETE FROM rvbbit.warren_jobs WHERE job_id = %s", (job_id,))
        rvbbit.execute("DELETE FROM rvbbit.warren_nodes WHERE name = %s", (node_name,))
