import os
import uuid

import psycopg


RVBBIT_DSN = os.environ.get(
    "RVBBIT_DSN", "postgresql://postgres:rvbbit@pg-rvbbit:5432/bench"
)


def _rows(conn, table):
    return conn.execute(
        f"""
        SELECT id,
               term_ids::text,
               scores::text,
               labels::text,
               pg_typeof(term_ids)::text,
               pg_typeof(scores)::text,
               pg_typeof(labels)::text,
               array_dims(term_ids),
               array_dims(scores)
          FROM {table}
         WHERE id >= 1
         ORDER BY id
        """
    ).fetchall()


def test_postgres_arrays_round_trip_through_native_acceleration(rvbbit):
    """OID 1009 and sibling arrays remain real typed arrays after Parquet."""
    table = f"array_roundtrip_{uuid.uuid4().hex[:8]}"
    try:
        rvbbit.execute(
            f"""
            CREATE TABLE {table} (
                id integer PRIMARY KEY,
                term_ids text[],
                scores integer[],
                labels varchar(12)[]
            ) USING rvbbit
            """
        )
        rvbbit.execute(
            f"""
            INSERT INTO {table} VALUES
                (1,
                 ARRAY['alpha', 'comma,value', 'quote"value', NULL]::text[],
                 ARRAY[1, NULL, 3]::integer[],
                 ARRAY['new', 'open']::varchar(12)[]),
                (2,
                 ARRAY[]::text[],
                 ARRAY[[1, 2], [3, 4]]::integer[],
                 NULL),
                (3,
                 '[0:2]={{zero,NULL,"comma,value"}}'::text[],
                 NULL,
                 ARRAY[]::varchar(12)[]),
                (4, NULL, ARRAY[]::integer[], ARRAY['closed']::varchar(12)[])
            """
        )

        # Capture PostgreSQL's canonical values, including dimensions/lower
        # bounds, before any accelerator is authoritative.
        expected = _rows(rvbbit, table)
        result = rvbbit.execute(
            f"SELECT rvbbit.refresh_acceleration('{table}'::regclass, false)"
        ).fetchone()[0]
        assert result["status"] == "ok"
        assert result["rows_written"] == 4

        # Use a fresh reader backend after publication, matching pooled/live
        # query behavior and guaranteeing this assertion exercises committed
        # row-group metadata rather than the writer's pre-build plan cache.
        with psycopg.connect(RVBBIT_DSN, autocommit=True) as accelerated:
            accelerated.execute("SET rvbbit.route_force_candidate = 'rvbbit_native'")
            plan = "\n".join(
                row[0]
                for row in accelerated.execute(
                    f"EXPLAIN (COSTS OFF) SELECT id, term_ids, scores, labels "
                    f"FROM {table} WHERE id >= 1"
                ).fetchall()
            )
            assert "RvbbitParquetScan" in plan

            # The custom scan must reconstruct each declared array type,
            # rather than leak canonical strings into the PostgreSQL executor.
            assert _rows(accelerated, table) == expected
            assert accelerated.execute(
                f"""
                SELECT id, term_ids[1], term_ids[2], array_length(scores, 2)
                  FROM {table}
                 WHERE term_ids @> ARRAY['alpha']::text[]
                """
            ).fetchall() == [(1, "alpha", "comma,value", None)]
            assert accelerated.execute(
                f"SELECT term_ids[0], term_ids[2], array_dims(term_ids) "
                f"FROM {table} WHERE id = 3"
            ).fetchone() == ("zero", "comma,value", "[0:2]")
            assert accelerated.execute(
                f"SELECT scores[2][1], array_dims(scores) FROM {table} WHERE id = 2"
            ).fetchone() == (3, "[1:2][1:2]")
    finally:
        rvbbit.execute("RESET rvbbit.route_force_candidate")
        rvbbit.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
