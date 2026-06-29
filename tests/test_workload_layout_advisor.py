import uuid


def _seed_workload_shape(rvbbit, shape_key, shape_family, sql, executions=5, elapsed_ms=120.0):
    rvbbit.execute(
        """
        INSERT INTO rvbbit.route_shape_samples (shape_key, shape_family, sql)
        VALUES (%s, %s, %s)
        ON CONFLICT (shape_key) DO UPDATE
        SET shape_family = EXCLUDED.shape_family,
            sql = EXCLUDED.sql
        """,
        (shape_key, shape_family, sql),
    )
    rvbbit.execute(
        """
        INSERT INTO rvbbit.route_executions (
            backend_pid, database_name, role_name, query_hash, shape_key, shape_family,
            route, candidate, profile_source, route_source, reason, elapsed_ms,
            rows_returned, status, features, route_doc
        )
        SELECT pg_backend_pid(), current_database(), current_user, %s, %s, %s,
               'native', 'rvbbit_native', 'none', 'eligibility', 'pytest workload seed',
               %s, 1, 'ok', '{}'::jsonb, '{}'::jsonb
        FROM generate_series(1, %s)
        """,
        (shape_key, shape_key, shape_family, elapsed_ms, executions),
    )


def test_workload_layout_advisor_recommends_and_persists(rvbbit, temp_table):
    rvbbit.execute("SELECT rvbbit.migrate()")
    rvbbit.execute(
        f"""
        CREATE TABLE {temp_table} (
            user_id int NOT NULL,
            region text NOT NULL,
            event_date date NOT NULL,
            amount int NOT NULL
        ) USING rvbbit
        """
    )
    rvbbit.execute(
        f"""
        INSERT INTO {temp_table}
        SELECT g, 'r' || (g % 4), DATE '2026-01-01' + (g % 30), g % 100
        FROM generate_series(1, 1000) AS g
        """
    )
    rvbbit.execute(f"ANALYZE {temp_table}")

    shape_key = f"pytest_workload_{uuid.uuid4().hex[:8]}"
    sql = (
        f"SELECT region, count(*) FROM {temp_table} "
        f"WHERE event_date >= DATE '2026-01-15' "
        f"GROUP BY region ORDER BY region"
    )
    _seed_workload_shape(rvbbit, shape_key, "pytest_workload", sql)

    result = rvbbit.execute(
        f"SELECT rvbbit.recommend_workload_layouts('{temp_table}'::regclass, 24, 1, 8, true)"
    ).fetchone()[0]
    assert result["ok"] is True
    layouts = {(r["layout_kind"], r["column_name"]) for r in result["recommendations"]}
    assert ("cluster", "event_date") in layouts
    assert ("hive", "region") in layouts

    rows = rvbbit.execute(
        f"""
        SELECT layout_kind, column_name, status, observations > 0, score > 0
        FROM rvbbit.workload_layout_recommendation_status
        WHERE table_oid = '{temp_table}'::regclass
        """
    ).fetchall()
    assert ("cluster", "event_date", "candidate", True, True) in rows
    assert ("hive", "region", "candidate", True, True) in rows


def test_accepted_workload_layouts_build_ready_variants(rvbbit, temp_table):
    rvbbit.execute("SELECT rvbbit.migrate()")
    rvbbit.execute(
        f"""
        CREATE TABLE {temp_table} (
            user_id int NOT NULL,
            region text NOT NULL,
            event_date date NOT NULL,
            amount int NOT NULL
        ) USING rvbbit
        """
    )
    rvbbit.execute(
        f"""
        INSERT INTO {temp_table}
        SELECT g, 'r' || (g % 4), DATE '2026-01-01' + (g % 30), g % 100
        FROM generate_series(1, 512) AS g
        """
    )
    rvbbit.execute(f"ANALYZE {temp_table}")

    shape_key = f"pytest_workload_{uuid.uuid4().hex[:8]}"
    sql = (
        f"SELECT region, count(*) FROM {temp_table} "
        f"WHERE event_date >= DATE '2026-01-15' "
        f"GROUP BY region ORDER BY region"
    )
    _seed_workload_shape(rvbbit, shape_key, "pytest_workload", sql)
    rvbbit.execute(
        f"SELECT rvbbit.recommend_workload_layouts('{temp_table}'::regclass, 24, 1, 8, true)"
    ).fetchone()

    accepted_cluster = rvbbit.execute(
        f"SELECT rvbbit.accept_workload_layout('{temp_table}'::regclass, 'cluster', 'event_date')"
    ).fetchone()[0]
    accepted_hive = rvbbit.execute(
        f"SELECT rvbbit.accept_workload_layout('{temp_table}'::regclass, 'hive', 'region')"
    ).fetchone()[0]
    assert accepted_cluster["status"] == "accepted"
    assert accepted_hive["status"] == "accepted"

    built = rvbbit.execute(
        f"SELECT rvbbit.build_accepted_workload_layouts('{temp_table}'::regclass)"
    ).fetchone()[0]
    assert built["status"] == "ok"
    assert built["accepted_layouts"] == 2
    assert built["ready_layouts"] == 2

    status = rvbbit.execute(
        f"""
        SELECT layout, layout_kind, partition_key, status, actual_rows
        FROM rvbbit.layout_variant_status_for('{temp_table}'::regclass)
        """
    ).fetchall()
    assert ("cluster:event_date", "cluster", "event_date", "ready", 512) in status
    assert ("hive:region", "hive", "region", "ready", 512) in status


def test_build_accepted_workload_layouts_marks_ready_when_base_is_current(rvbbit, temp_table):
    rvbbit.execute("SELECT rvbbit.migrate()")
    rvbbit.execute(
        f"""
        CREATE TABLE {temp_table} (
            user_id int NOT NULL,
            region text NOT NULL,
            event_date date NOT NULL,
            amount int NOT NULL
        ) USING rvbbit
        """
    )
    rvbbit.execute(
        f"""
        INSERT INTO {temp_table}
        SELECT g, 'r' || (g % 4), DATE '2026-01-01' + (g % 30), g % 100
        FROM generate_series(1, 384) AS g
        """
    )
    rvbbit.execute(f"ANALYZE {temp_table}")

    rvbbit.execute(f"SELECT rvbbit.rebuild_acceleration('{temp_table}'::regclass, false)")
    assert (
        rvbbit.execute(
            f"SELECT count(*) > 0 FROM rvbbit.row_groups WHERE table_oid = '{temp_table}'::regclass"
        ).fetchone()[0]
        is True
    )

    rvbbit.execute(
        f"SELECT rvbbit.accept_workload_layout('{temp_table}'::regclass, 'cluster', 'event_date')"
    )
    rvbbit.execute(
        f"SELECT rvbbit.accept_workload_layout('{temp_table}'::regclass, 'hive', 'region')"
    )
    assert (
        rvbbit.execute(
            f"SELECT rvbbit.workload_layout_variants_pending('{temp_table}'::regclass)"
        ).fetchone()[0]
        is True
    )

    built = rvbbit.execute(
        f"SELECT rvbbit.build_accepted_workload_layouts('{temp_table}'::regclass)"
    ).fetchone()[0]
    assert built["status"] == "ok"
    assert built["accepted_layouts"] == 2
    assert built["ready_layouts"] == 2
    assert built["layout_rows"] >= 384

    status = rvbbit.execute(
        f"""
        SELECT layout, status, layout_status, layout_rows
        FROM rvbbit.workload_layout_recommendation_status
        WHERE table_oid = '{temp_table}'::regclass
        ORDER BY layout
        """
    ).fetchall()
    assert ("cluster:event_date", "accepted", "ready", 384) in status
    assert ("hive:region", "accepted", "ready", 384) in status
    assert (
        rvbbit.execute(
            f"SELECT rvbbit.workload_layout_variants_pending('{temp_table}'::regclass)"
        ).fetchone()[0]
        is False
    )
