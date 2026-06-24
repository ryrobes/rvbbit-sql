import datetime as dt
import os
from pathlib import Path


def _heap_scalar(conn, sql):
    conn.execute("SET rvbbit.force_heap_scan = on")
    try:
        return conn.execute(sql).fetchone()
    finally:
        conn.execute("RESET rvbbit.force_heap_scan")


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


def test_direct_canonical_parquet_import_uses_threaded_writer_and_logs_timing(
    rvbbit, temp_table
):
    import pyarrow as pa
    import pyarrow.parquet as pq

    source_dir = Path(os.environ.get("RVBBIT_DIRECT_ACCEL_DIR", "/rvbbit_import"))
    source_dir.mkdir(parents=True, exist_ok=True)
    source_path = source_dir / f"{temp_table}.parquet"
    n_rows = 2000
    epoch_start = int(dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc).timestamp())
    source = pa.table(
        {
            "id": pa.array(range(n_rows), type=pa.int32()),
            "label": pa.array([f"label-{i % 17}" for i in range(n_rows)]),
            "day": pa.array(
                [dt.date(2024, 1, 1) + dt.timedelta(days=i % 31) for i in range(n_rows)],
                type=pa.date32(),
            ),
            "ts": pa.array([epoch_start + i for i in range(n_rows)], type=pa.int64()),
        }
    )
    pq.write_table(source, source_path, compression="zstd", row_group_size=500)

    try:
        rvbbit.execute(
            f"""
            CREATE TABLE {temp_table} (
                id int,
                label text,
                day date,
                ts timestamp
            ) USING rvbbit
            """
        )
        rvbbit.execute(
            f"""
            INSERT INTO {temp_table}
            SELECT g,
                   'label-' || (g % 17)::text,
                   DATE '2024-01-01' + ((g % 31)::int),
                   TIMESTAMP '2024-01-01 00:00:00'
                     + (g::bigint * INTERVAL '1 second')
            FROM generate_series(0, {n_rows - 1}) AS g
            """
        )

        rvbbit.execute("SET rvbbit.accel_identity_map = 'off'")
        rvbbit.execute("SET rvbbit.compact_scan_chunk_rows = '500'")
        rvbbit.execute("SET rvbbit.compact_writer_threads = '4'")
        rvbbit.execute("SET rvbbit.direct_accel_metadata_profile = 'minimal'")
        rvbbit.execute("SET rvbbit.import_epoch_seconds_columns = 'ts'")
        doc = rvbbit.execute(
            f"""
            SELECT rvbbit.import_canonical_parquet_chunks(
                '{temp_table}'::regclass,
                ARRAY[%s],
                false
            )
            """,
            (str(source_path),),
        ).fetchone()[0]

        assert doc["status"] == "ok"
        assert doc["rows_written"] == n_rows
        assert doc["row_groups_written"] == 4
        assert doc["metadata_profile"] == "minimal"
        assert doc["timing"]["writer_seconds_sum"] > 0
        assert "source_canonicalize_seconds" in doc["timing"]

        row_groups = rvbbit.execute(
            f"""
            SELECT count(*)::int, sum(n_rows)::int
            FROM rvbbit.row_groups
            WHERE table_oid = '{temp_table}'::regclass
            """
        ).fetchone()
        assert row_groups == (4, n_rows)

        sql = (
            f"SELECT count(*)::int, sum(id)::bigint, min(day), max(ts) "
            f"FROM {temp_table}"
        )
        assert rvbbit.execute(sql).fetchone() == _heap_scalar(rvbbit, sql)

        phase = rvbbit.execute(
            f"""
            SELECT details->>'writer_threads',
                   details->>'chunk_rows',
                   details->>'metadata_profile',
                   details->'import_timing'
            FROM rvbbit.acceleration_operation_phases
            WHERE table_oid = '{temp_table}'::regclass
              AND phase = 'canonical_delta_import'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        assert phase[0] == "4"
        assert phase[1] == "500"
        assert phase[2] == "minimal"
        assert phase[3]["writer_seconds_sum"] > 0
    finally:
        try:
            source_path.unlink()
        except FileNotFoundError:
            pass


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
