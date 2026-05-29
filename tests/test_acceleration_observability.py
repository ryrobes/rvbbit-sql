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
        SELECT phase, layout_kind, partition_key, status, rows_written
        FROM rvbbit.acceleration_phase_log_for('{temp_table}'::regclass)
    """).fetchall()
    assert any(p[0] == "canonical_delta_export" and p[3] == "ok" for p in phases)
    assert any(
        p[0] == "layout_variant_rebuild"
        and p[1] == "hive"
        and p[2] == "bucket"
        and p[3] == "ok"
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
        SELECT phase, layout_kind, partition_key, status, rows_written
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
