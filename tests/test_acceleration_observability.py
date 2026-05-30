def test_refresh_phase_log_and_hive_delta_append(rvbbit, temp_table):
    rvbbit.execute(f"""
        CREATE TABLE {temp_table} (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            bucket int NOT NULL,
            payload text NOT NULL
        ) USING rvbbit
    """)
    rvbbit.execute(f"""
        INSERT INTO {temp_table} (bucket, payload)
        SELECT g % 4, 'payload ' || g
        FROM generate_series(1, 24) g
    """)
    rvbbit.execute(f"ANALYZE {temp_table}")

    rvbbit.execute("SET rvbbit.compact_hive_layout = 'on'")
    rvbbit.execute("SET rvbbit.compact_hive_keys = 'bucket'")
    rvbbit.execute("SET rvbbit.compact_hive_variants = '1'")

    rvbbit.execute(
        f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, true)"
    ).fetchone()

    phases = rvbbit.execute(f"""
        SELECT phase, layout_kind, partition_key, status, rows_written,
               details->>'source', details->>'metadata_profile'
        FROM rvbbit.acceleration_phase_log_for('{temp_table}'::regclass)
    """).fetchall()
    assert any(p[0] == "canonical_delta_export" and p[3] == "ok" for p in phases)
    assert any(
        p[0] == "layout_variant_rebuild"
        and p[1] == "hive"
        and p[2] == "bucket"
        and p[3] == "ok"
        and p[5] == "canonical_parquet"
        and p[6] == "minimal"
        for p in phases
    )

    rvbbit.execute(f"""
        INSERT INTO {temp_table} (bucket, payload)
        SELECT g % 4, 'delta ' || g
        FROM generate_series(25, 32) g
    """)
    rvbbit.execute(
        f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, true)"
    ).fetchone()

    phases = rvbbit.execute(f"""
        SELECT phase, layout_kind, partition_key, status, rows_written,
               details->>'source', details->>'metadata_profile'
        FROM rvbbit.acceleration_phase_log_for('{temp_table}'::regclass)
    """).fetchall()
    assert any(
        p[0] == "layout_variant_delta_append"
        and p[1] == "hive"
        and p[2] == "bucket"
        and p[3] == "ok"
        and p[4] == 8
        for p in phases
    )

    status = rvbbit.execute(f"""
        SELECT layout_kind, partition_key, status, expected_rows, actual_rows
        FROM rvbbit.layout_variant_status_for('{temp_table}'::regclass)
    """).fetchall()
    assert ("hive", "bucket", "ready", 32, 32) in status


def test_vortex_format_variant_and_forced_datafusion_route(rvbbit, temp_table):
    rvbbit.execute(f"""
        CREATE TABLE {temp_table} (
            id int NOT NULL,
            bucket int NOT NULL,
            payload text NOT NULL
        ) USING rvbbit
    """)
    rvbbit.execute(f"""
        INSERT INTO {temp_table}
        SELECT g, g % 3, 'payload ' || g
        FROM generate_series(1, 30) g
    """)
    rvbbit.execute(f"ANALYZE {temp_table}")

    try:
        rvbbit.execute("SET rvbbit.compact_hive_layout = 'off'")
        rvbbit.execute("SET rvbbit.compact_vortex_layout = 'on'")
        rvbbit.execute(
            f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, true)"
        ).fetchone()

        status = rvbbit.execute(f"""
            SELECT layout, layout_kind, status, expected_rows, actual_rows, file_count
            FROM rvbbit.layout_variant_status_for('{temp_table}'::regclass)
        """).fetchall()
        assert any(
            row[0] == "vortex_scan"
            and row[1] == "vortex"
            and row[2] == "ready"
            and row[3] == 30
            and row[4] == 30
            and row[5] >= 1
            for row in status
        )

        phases = rvbbit.execute(f"""
            SELECT phase, layout_kind, status, rows_written,
                   details->>'source', details->>'file_extension'
            FROM rvbbit.acceleration_phase_log_for('{temp_table}'::regclass)
        """).fetchall()
        assert any(
            row[0] in {"format_variant_rebuild", "format_variant_delta_append"}
            and row[1] == "vortex"
            and row[2] == "ok"
            and row[3] == 30
            and row[4] == "canonical_parquet"
            and row[5] == "vortex"
            for row in phases
        )

        sql = (
            f"SELECT bucket, sum(id)::bigint AS total "
            f"FROM {temp_table} GROUP BY bucket ORDER BY bucket"
        )
        rvbbit.execute("SET rvbbit.duck_backend = 'on'")
        rvbbit.execute("SET rvbbit.df_inprocess = 'on'")
        rvbbit.execute("SET rvbbit.route_force_candidate = 'datafusion_vortex'")
        forced = rvbbit.execute(
            "SELECT rvbbit.route_explain(%s)", (sql,)
        ).fetchone()[0]
        assert forced["chosen_candidate"] == "datafusion_vortex"
        assert "vortex" in forced["reason"].lower()
        assert rvbbit.execute(sql).fetchall() == [(0, 165), (1, 145), (2, 155)]
    finally:
        rvbbit.execute("SET rvbbit.route_force_candidate = ''")
        rvbbit.execute("SET rvbbit.compact_vortex_layout = 'off'")


def test_vortex_route_skips_temporal_tables(rvbbit, temp_table):
    rvbbit.execute(f"""
        CREATE TABLE {temp_table} (
            id int NOT NULL,
            observed_on date NOT NULL,
            bucket int NOT NULL
        ) USING rvbbit
    """)
    rvbbit.execute(f"""
        INSERT INTO {temp_table}
        SELECT g, date '2024-01-01' + (g % 5), g % 3
        FROM generate_series(1, 30) g
    """)
    rvbbit.execute(f"ANALYZE {temp_table}")

    try:
        rvbbit.execute("SET rvbbit.compact_vortex_layout = 'on'")
        rvbbit.execute(
            f"SELECT rvbbit.refresh_acceleration('{temp_table}'::regclass, true)"
        ).fetchone()

        rvbbit.execute("SET rvbbit.route_force_candidate = 'datafusion_vortex'")
        non_temporal = rvbbit.execute(
            f"SELECT rvbbit.route_explain('SELECT count(*) FROM {temp_table} "
            "WHERE bucket = 1')"
        ).fetchone()[0]
        assert non_temporal["chosen_candidate"] == "datafusion_vortex"

        forced = rvbbit.execute(
            f"SELECT rvbbit.route_explain('SELECT count(*) FROM {temp_table} "
            "WHERE observed_on >= DATE ''2024-01-02''')"
        ).fetchone()[0]
        assert forced["chosen_candidate"] == "rvbbit_native"
        assert forced["route_source"] == "forced-unavailable"
        assert "temporal pruning" in forced["reason"].lower()
    finally:
        rvbbit.execute("SET rvbbit.route_force_candidate = ''")
        rvbbit.execute("SET rvbbit.compact_vortex_layout = 'off'")
