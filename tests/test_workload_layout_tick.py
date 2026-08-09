import time
import uuid

import psycopg
import pytest

from conftest import RVBBIT_DSN


MAINTENANCE_GATE_CLASS = 1381187156
HEAVY_LANE_KEY = 8
TABLE_LOCK_CLASS = 1380336724


def _table_lock_key(table_oid: int) -> int:
    return (TABLE_LOCK_CLASS << 32) | table_oid


def _make_workload_candidate(conn, name: str, layouts=("cluster", "hive")) -> int:
    conn.execute(
        f"""
        CREATE TABLE {name} (
            id integer PRIMARY KEY,
            region text NOT NULL,
            event_date date NOT NULL,
            amount integer NOT NULL
        ) USING rvbbit
        """
    )
    conn.execute(
        f"""
        INSERT INTO {name}
        SELECT g,
               'r' || (g % 4),
               DATE '2026-01-01' + (g % 30),
               g % 100
          FROM generate_series(1, 256) AS g
        """
    )
    conn.execute(f"ANALYZE {name}")
    conn.execute(f"SELECT rvbbit.rebuild_acceleration('{name}'::regclass, false)")
    if "cluster" in layouts:
        conn.execute(
            f"SELECT rvbbit.accept_workload_layout("
            f"'{name}'::regclass, 'cluster', 'event_date')"
        )
    if "hive" in layouts:
        conn.execute(
            f"SELECT rvbbit.accept_workload_layout("
            f"'{name}'::regclass, 'hive', 'region')"
        )
    return conn.execute(f"SELECT '{name}'::regclass::oid").fetchone()[0]


def test_workload_layout_worker_repairs_only_non_vortex_variants(rvbbit):
    name = f"layout_tick_{uuid.uuid4().hex[:8]}"
    try:
        rvbbit.execute("SELECT rvbbit.migrate()")
        rvbbit.execute("SET rvbbit.compact_vortex_layout = 'on'")
        table_oid = _make_workload_candidate(rvbbit, name)

        candidate = rvbbit.execute(
            """
            SELECT accepted_layouts, pending_layouts, pending_layout_names
              FROM rvbbit.workload_layout_tick_candidates
             WHERE table_oid = %s
            """,
            (table_oid,),
        ).fetchone()
        assert candidate == (2, 2, ["cluster:event_date", "hive:region"])

        # The slot contract is ready for a later wider scheduler cohort even
        # though the seeded scheduler defaults to one worker.
        planned = rvbbit.execute(
            """
            SELECT executed, status, target_layouts
              FROM rvbbit.workload_layout_tick_worker(3, 3, true)
             WHERE table_oid = %s
            """,
            (table_oid,),
        ).fetchone()
        assert planned == (
            False,
            "planned",
            ["cluster:event_date", "hive:region"],
        )

        built = rvbbit.execute(
            """
            SELECT executed, status, rows_written
              FROM rvbbit.workload_layout_tick_worker(3, 3, false)
             WHERE table_oid = %s
            """,
            (table_oid,),
        ).fetchone()
        assert built[0:2] == (True, "ok")
        assert built[2] >= 512

        status = rvbbit.execute(
            """
            SELECT layout, status, actual_rows
              FROM rvbbit.layout_variant_status
             WHERE table_oid = %s
             ORDER BY layout
            """,
            (table_oid,),
        ).fetchall()
        assert status == [
            ("cluster:event_date", "ready", 256),
            ("hive:region", "ready", 256),
        ]
        assert all(layout != "vortex_scan" for layout, _, _ in status)
        assert rvbbit.execute(
            "SELECT rvbbit.workload_layout_variants_pending(%s)",
            (table_oid,),
        ).fetchone() == (False,)
        assert rvbbit.execute(
            "SELECT count(*) FROM rvbbit.workload_layout_tick_candidates "
            "WHERE table_oid = %s",
            (table_oid,),
        ).fetchone() == (0,)

        run = rvbbit.execute(
            """
            SELECT worker_slot, worker_count, status, target_layouts
              FROM rvbbit.workload_layout_tick_runs
             WHERE table_oid = %s
             ORDER BY id DESC
             LIMIT 1
            """,
            (table_oid,),
        ).fetchone()
        assert run == (
            3,
            3,
            "ok",
            ["cluster:event_date", "hive:region"],
        )
    finally:
        rvbbit.execute(f"DROP TABLE IF EXISTS {name} CASCADE")


def test_canonical_change_reopens_accepted_layout_candidate(rvbbit):
    name = f"layout_stale_{uuid.uuid4().hex[:8]}"
    try:
        rvbbit.execute("SELECT rvbbit.migrate()")
        table_oid = _make_workload_candidate(rvbbit, name, layouts=("hive",))
        assert rvbbit.execute(
            "SELECT status FROM rvbbit.workload_layout_tick(false) "
            "WHERE table_oid = %s",
            (table_oid,),
        ).fetchone() == ("ok",)

        rvbbit.execute(
            f"""
            INSERT INTO {name}
            SELECT g, 'new', DATE '2026-02-01', g
              FROM generate_series(257, 288) AS g
            """
        )
        rvbbit.execute(
            f"SELECT rvbbit.refresh_acceleration('{name}'::regclass, false)"
        )

        stale = rvbbit.execute(
            """
            SELECT pending_layouts, target_rows
              FROM rvbbit.workload_layout_tick_candidates
             WHERE table_oid = %s
            """,
            (table_oid,),
        ).fetchone()
        assert stale == (1, 288)
        assert rvbbit.execute(
            "SELECT rvbbit.workload_layout_variants_pending(%s)",
            (table_oid,),
        ).fetchone() == (True,)
    finally:
        rvbbit.execute(f"DROP TABLE IF EXISTS {name} CASCADE")


def test_workload_layout_lane_skips_heavy_maintenance_without_waiting(rvbbit):
    name = f"layout_gate_{uuid.uuid4().hex[:8]}"
    try:
        rvbbit.execute("SELECT rvbbit.migrate()")
        _make_workload_candidate(rvbbit, name, layouts=("hive",))

        with psycopg.connect(RVBBIT_DSN, autocommit=False) as holder:
            holder.execute(
                "SELECT pg_advisory_xact_lock(%s, %s)",
                (MAINTENANCE_GATE_CLASS, HEAVY_LANE_KEY),
            )
            started = time.monotonic()
            rows = rvbbit.execute(
                "SELECT * FROM rvbbit.workload_layout_tick_worker(1, 1, false)"
            ).fetchall()
            elapsed = time.monotonic() - started
            holder.rollback()

        assert rows == []
        assert elapsed < 1.0
    finally:
        rvbbit.execute(f"DROP TABLE IF EXISTS {name} CASCADE")


def test_workload_layout_worker_steals_past_a_claimed_table(rvbbit):
    suffix = uuid.uuid4().hex[:8]
    names = [f"layout_claim_a_{suffix}", f"layout_claim_b_{suffix}"]
    try:
        rvbbit.execute("SELECT rvbbit.migrate()")
        oids = [
            _make_workload_candidate(rvbbit, name, layouts=("hive",))
            for name in names
        ]

        # Lock whichever table this slot would prefer first. It must continue
        # scanning and claim the other candidate instead of waiting or no-oping.
        preferred = sorted(oids, key=lambda oid: (oid % 2 + 1 != 1, oid))[0]
        other = next(oid for oid in oids if oid != preferred)
        with psycopg.connect(RVBBIT_DSN, autocommit=False) as holder:
            holder.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (_table_lock_key(preferred),),
            )
            built = rvbbit.execute(
                """
                SELECT table_oid, executed, status
                  FROM rvbbit.workload_layout_tick_worker(1, 2, false)
                 WHERE table_oid = ANY(%s::oid[])
                """,
                (oids,),
            ).fetchone()
            holder.rollback()

        assert built == (other, True, "ok")
        assert rvbbit.execute(
            "SELECT rvbbit.workload_layout_variants_pending(%s)",
            (preferred,),
        ).fetchone() == (True,)
    finally:
        for name in reversed(names):
            rvbbit.execute(f"DROP TABLE IF EXISTS {name} CASCADE")


def test_reconciliation_and_log_retention_patches_are_present(rvbbit):
    rvbbit.execute("SELECT rvbbit.migrate()")
    maintain_def, reap_def = rvbbit.execute(
        """
        SELECT pg_get_functiondef(
                   'rvbbit.maintain_storage(bigint,boolean)'::regprocedure
               ),
               pg_get_functiondef('rvbbit.reap_logs(interval)'::regprocedure)
        """
    ).fetchone()
    assert "workload_layout_variants_pending" in maintain_def
    assert "workload_layout_tick_runs" in reap_def


def test_workload_layout_worker_validates_slots(rvbbit):
    rvbbit.execute("SELECT rvbbit.migrate()")
    with pytest.raises(psycopg.errors.RaiseException, match="between 1 and 8"):
        rvbbit.execute(
            "SELECT * FROM rvbbit.workload_layout_tick_worker(1, 9, true)"
        )
    with pytest.raises(psycopg.errors.RaiseException, match="worker_slot"):
        rvbbit.execute(
            "SELECT * FROM rvbbit.workload_layout_tick_worker(0, 2, true)"
        )
