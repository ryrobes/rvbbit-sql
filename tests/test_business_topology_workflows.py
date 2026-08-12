from __future__ import annotations

import uuid

from psycopg import sql


def test_business_topology_workflow_is_sql_leased_idempotent_and_observable(rvbbit):
    suffix = uuid.uuid4().hex[:10]
    table_name = f"bt_workflow_{suffix}"
    relation = f"public.{table_name}"
    worker_id = f"pytest-topology-{suffix}"
    run_ids: list[uuid.UUID] = []

    try:
        rvbbit.execute(
            sql.SQL("CREATE TABLE {} (id bigint, label text)").format(
                sql.Identifier(table_name)
            )
        )
        first = rvbbit.execute(
            """
            SELECT rvbbit.business_topology_start_workflow(
                p_relations=>ARRAY[%s],p_sample_rows=>64,
                p_max_work_items=>10,p_max_llm_calls=>2,p_run_name=>%s
            )
            """,
            (relation, f"Workflow {suffix}"),
        ).fetchone()[0]
        run_ids.append(first)
        repeated = rvbbit.execute(
            "SELECT rvbbit.business_topology_start_workflow(p_relations=>ARRAY[%s])",
            (relation,),
        ).fetchone()[0]
        assert repeated == first

        rvbbit.execute(
            "SELECT rvbbit.business_topology_register_workflow_worker(%s,'pytest-v1')",
            (worker_id,),
        )
        claimed = rvbbit.execute(
            "SELECT run_id,parameters FROM rvbbit.business_topology_claim_workflow(%s,300)",
            (worker_id,),
        ).fetchone()
        assert claimed[0] == first
        assert claimed[1]["relations"] == [relation]

        assert rvbbit.execute(
            """
            SELECT rvbbit.business_topology_update_workflow(
                %s,%s,'planning',
                '{"population_count":7,"work_item_count":11}'::jsonb,300
            )
            """,
            (first, worker_id),
        ).fetchone()[0]
        status = rvbbit.execute(
            """
            SELECT status,phase,population_count,work_item_count,any_worker_online
              FROM rvbbit.business_topology_workflow_status WHERE run_id=%s
            """,
            (first,),
        ).fetchone()
        assert status == ("running", "planning", 7, 11, True)

        assert rvbbit.execute(
            "SELECT rvbbit.business_topology_cancel_workflow(%s)", (first,)
        ).fetchone()[0] == "cancel_requested"
        rvbbit.execute(
            "SELECT rvbbit.business_topology_fail_workflow(%s,%s,'cancelled','{}'::jsonb)",
            (first, worker_id),
        )
        assert rvbbit.execute(
            "SELECT status FROM rvbbit.business_topology_workflow_runs WHERE run_id=%s",
            (first,),
        ).fetchone()[0] == "cancelled"

        second = rvbbit.execute(
            """
            SELECT rvbbit.business_topology_start_workflow(
                p_relations=>ARRAY[%s],p_sample_rows=>64,
                p_max_work_items=>10,p_max_llm_calls=>2,p_run_name=>'Completion test'
            )
            """,
            (relation,),
        ).fetchone()[0]
        run_ids.append(second)
        assert second != first
        assert rvbbit.execute(
            "SELECT run_id FROM rvbbit.business_topology_claim_workflow(%s,300)",
            (worker_id,),
        ).fetchone()[0] == second
        rvbbit.execute(
            """
            SELECT rvbbit.business_topology_complete_workflow(
                %s,%s,
                '{"schema_version":"rvbbit.business-topology.workflow-result.v1",\
                  "bundles_staged":3}'::jsonb
            )
            """,
            (second, worker_id),
        )
        completed = rvbbit.execute(
            """
            SELECT status,phase,bundles_staged,worker_id,lease_expires_at
              FROM rvbbit.business_topology_workflow_status WHERE run_id=%s
            """,
            (second,),
        ).fetchone()
        assert completed == ("completed", "completed", 3, None, None)
    finally:
        if run_ids:
            rvbbit.execute(
                "DELETE FROM rvbbit.business_topology_workflow_runs WHERE run_id=ANY(%s)",
                (run_ids,),
            )
        rvbbit.execute(
            "DELETE FROM rvbbit.business_topology_workflow_workers WHERE worker_id=%s",
            (worker_id,),
        )
        rvbbit.execute(
            sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(sql.Identifier(table_name))
        )
