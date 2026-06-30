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


def _cleanup_workload_shape(rvbbit, shape_key):
    rvbbit.execute("DELETE FROM rvbbit.route_executions WHERE shape_key = %s", (shape_key,))
    rvbbit.execute("DELETE FROM rvbbit.route_shape_samples WHERE shape_key = %s", (shape_key,))


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

    try:
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
    finally:
        _cleanup_workload_shape(rvbbit, shape_key)


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
    try:
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
    finally:
        _cleanup_workload_shape(rvbbit, shape_key)


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


def test_system_learning_brain_provider_indexes_workload_artifacts(rvbbit, temp_table):
    rvbbit.execute("SELECT rvbbit.migrate()")

    provider = rvbbit.execute(
        """
        SELECT provider, doc_type, jsonb_array_length(edge_map) AS edges
        FROM rvbbit.brain_doc_providers
        WHERE provider = 'rvbbit-system-learning'
        """
    ).fetchone()
    assert provider == ("rvbbit-system-learning", "system_learning", 6)

    source = rvbbit.execute(
        """
        SELECT label, kind, enabled, config->>'provider' AS provider
        FROM rvbbit.brain_sources
        WHERE label = 'RVBBIT System Learning'
        """
    ).fetchone()
    assert source == ("RVBBIT System Learning", "query", True, "rvbbit-system-learning")

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
        FROM generate_series(1, 256) AS g
        """
    )
    rvbbit.execute(f"ANALYZE {temp_table}")

    shape_key = f"pytest_learning_{uuid.uuid4().hex[:8]}"
    sql = (
        f"SELECT region, count(*) FROM {temp_table} "
        f"WHERE event_date >= DATE '2026-01-15' "
        f"GROUP BY region ORDER BY region"
    )
    _seed_workload_shape(rvbbit, shape_key, "pytest_learning", sql)
    try:
        rvbbit.execute(
            f"SELECT rvbbit.recommend_workload_layouts('{temp_table}'::regclass, 24, 1, 8, true)"
        )

        row = rvbbit.execute(
            f"""
            SELECT uri, title, props->>'object_type' AS kind, props->>'table' AS table_name,
                   props->>'column' AS column_name, body LIKE '%RVBBIT learned a workload layout recommendation.%' AS body_ok
            FROM rvbbit.system_learning_items
            WHERE props->>'object_type' = 'workload_layout'
              AND props->>'table' = '{temp_table}'::regclass::text
              AND props->>'column' = 'event_date'
            """
        ).fetchone()

        assert row is not None
        assert row[0].startswith("rvbbit:layout:")
        assert "cluster:event_date" in row[1]
        assert row[2:] == ("workload_layout", temp_table, "event_date", True)

        status = rvbbit.execute(
            """
            SELECT installed, source_id IS NOT NULL AS has_source, enabled, indexed_items >= 1 AS has_items
            FROM rvbbit.system_learning_brain_status
            """
        ).fetchone()
        assert status == (True, True, True, True)

        summary = rvbbit.execute(
            """
            SELECT object_type, items >= 1 AS has_items
            FROM rvbbit.system_learning_item_summary
            WHERE object_type IN ('workload_layout', 'route_shape', 'acceleration_state', 'operator')
            ORDER BY object_type
            """
        ).fetchall()
        assert ("workload_layout", True) in summary
        assert ("route_shape", True) in summary
    finally:
        _cleanup_workload_shape(rvbbit, shape_key)
