import uuid

import psycopg
import pytest

from conftest import RVBBIT_DSN


TABLE_LOCK_CLASS = 1380336724


def _table_lock_key(table_oid: int) -> int:
    return (TABLE_LOCK_CLASS << 32) | table_oid


def _make_scheduled_table(conn, name: str, build_baseline: bool) -> int:
    conn.execute(
        f"CREATE TABLE {name} (id integer PRIMARY KEY, payload text) USING rvbbit"
    )
    conn.execute(
        f"INSERT INTO {name} "
        "SELECT value, repeat(md5(value::text), 2) "
        "FROM generate_series(1, 2000) AS value"
    )
    conn.execute(f"ANALYZE {name}")
    conn.execute(
        f"""
        SELECT rvbbit.set_accel_policy(
            '{name}'::regclass,
            strategy => 'scheduled',
            min_interval_secs => 0,
            full_rebuild_drift_ratio => 2.0
        )
        """
    )
    if build_baseline:
        conn.execute(f"SELECT rvbbit.rebuild_acceleration('{name}'::regclass, false)")
        conn.execute(
            f"INSERT INTO {name} "
            "SELECT value, repeat(md5(value::text), 2) "
            "FROM generate_series(2001, 2100) AS value"
        )
        conn.execute(f"ANALYZE {name}")
    return conn.execute(f"SELECT '{name}'::regclass::oid").fetchone()[0]


def _make_vortex_candidate(conn, name: str) -> int:
    conn.execute("SET rvbbit.compact_vortex_layout = 'on'")
    conn.execute(
        f"CREATE TABLE {name} (id integer PRIMARY KEY, payload text) USING rvbbit"
    )
    conn.execute(
        f"INSERT INTO {name} "
        "SELECT value, md5(value::text) "
        "FROM generate_series(1, 256) AS value"
    )
    conn.execute(f"ANALYZE {name}")
    conn.execute(f"SELECT rvbbit.rebuild_acceleration('{name}'::regclass, false)")
    table_oid = conn.execute(f"SELECT '{name}'::regclass::oid").fetchone()[0]
    conn.execute(
        "SELECT rvbbit.enqueue_variant_build(%s::regclass, 'heavy_slot_test', 10)",
        (table_oid,),
    )
    return table_oid


def test_heavy_slot_pool_defaults_to_two_and_exposes_live_holders(rvbbit):
    rvbbit.execute("SELECT rvbbit.migrate()")
    assert rvbbit.execute(
        "SELECT rvbbit.accel_maintenance_heavy_slots()"
    ).fetchone() == (2,)

    with psycopg.connect(RVBBIT_DSN, autocommit=False) as first:
        with psycopg.connect(RVBBIT_DSN, autocommit=False) as second:
            with psycopg.connect(RVBBIT_DSN, autocommit=False) as third:
                assert first.execute(
                    "SELECT rvbbit._try_claim_accel_heavy_slot(2)"
                ).fetchone() == (1,)
                assert second.execute(
                    "SELECT rvbbit._try_claim_accel_heavy_slot(2)"
                ).fetchone() == (2,)
                assert third.execute(
                    "SELECT rvbbit._try_claim_accel_heavy_slot(2)"
                ).fetchone() == (0,)

                slots = rvbbit.execute(
                    """
                    SELECT heavy_slot, enabled, status, backend_pid IS NOT NULL
                      FROM rvbbit.accel_heavy_slot_activity
                     ORDER BY heavy_slot
                    """
                ).fetchall()
                assert slots[0:2] == [
                    (1, True, "busy", True),
                    (2, True, "busy", True),
                ]
                assert all(row[2] == "disabled" for row in slots[2:])

                third.rollback()
            second.rollback()
        first.rollback()

    assert rvbbit.execute(
        """
        SELECT count(*)
          FROM rvbbit.accel_heavy_slot_activity
         WHERE enabled AND status = 'free'
        """
    ).fetchone() == (2,)


def test_heavy_slot_limit_is_configurable_and_validated(rvbbit):
    rvbbit.execute("SELECT rvbbit.migrate()")
    original = rvbbit.execute(
        "SELECT rvbbit.accel_maintenance_heavy_slots()"
    ).fetchone()[0]
    try:
        configured = rvbbit.execute(
            "SELECT rvbbit.set_accel_maintenance_heavy_slots(3)"
        ).fetchone()[0]
        assert configured["heavy_slots"] == 3
        assert rvbbit.execute(
            "SELECT rvbbit.accel_maintenance_heavy_slots()"
        ).fetchone() == (3,)
        assert rvbbit.execute(
            """
            SELECT count(*)
              FROM rvbbit.accel_heavy_slot_activity
             WHERE enabled
            """
        ).fetchone() == (3,)

        with pytest.raises(psycopg.errors.RaiseException, match="between 1 and 8"):
            rvbbit.execute("SELECT rvbbit.set_accel_maintenance_heavy_slots(9)")
    finally:
        rvbbit.execute(
            "SELECT rvbbit.set_accel_maintenance_heavy_slots(%s)",
            (original,),
        )


def test_full_pool_does_not_starve_delta_refresh_work(rvbbit):
    suffix = uuid.uuid4().hex[:8]
    full_name = f"heavy_full_{suffix}"
    delta_name = f"heavy_delta_{suffix}"
    try:
        rvbbit.execute("SELECT rvbbit.migrate()")
        full_oid = _make_scheduled_table(rvbbit, full_name, build_baseline=True)
        rvbbit.execute(f"TRUNCATE TABLE {full_name}")
        rvbbit.execute(
            f"INSERT INTO {full_name} "
            "SELECT value, repeat(md5(value::text), 2) "
            "FROM generate_series(1, 2000) AS value"
        )
        rvbbit.execute(f"ANALYZE {full_name}")
        assert rvbbit.execute(
            "SELECT rvbbit.current_replacement_pending(%s::regclass)",
            (full_oid,),
        ).fetchone() == (True,)
        delta_oid = _make_scheduled_table(rvbbit, delta_name, build_baseline=True)

        with psycopg.connect(RVBBIT_DSN, autocommit=False) as first:
            with psycopg.connect(RVBBIT_DSN, autocommit=False) as second:
                assert first.execute(
                    "SELECT rvbbit._try_claim_accel_heavy_slot(2)"
                ).fetchone() == (1,)
                assert second.execute(
                    "SELECT rvbbit._try_claim_accel_heavy_slot(2)"
                ).fetchone() == (2,)

                rows = rvbbit.execute(
                    """
                    SELECT table_oid::oid, action, executed, status, reason
                      FROM rvbbit.accel_tick_worker(1, 1, false, 1)
                     WHERE table_oid = ANY(%s::oid[])
                    """,
                    ([full_oid, delta_oid],),
                ).fetchall()

                second.rollback()
                full_built = rvbbit.execute(
                    """
                    SELECT action, executed, status
                      FROM rvbbit.accel_tick_worker(1, 1, false, 1)
                     WHERE table_oid = %s
                    """,
                    (full_oid,),
                ).fetchone()
                assert full_built == ("full", True, "ok")
                heavy_details = rvbbit.execute(
                    """
                    SELECT details ->> 'heavy_slot',
                           details ->> 'heavy_slot_limit'
                      FROM rvbbit.accel_activity_log
                     WHERE lane = 'freshness'
                       AND event_type = 'table_finished'
                       AND table_oid = %s
                     ORDER BY id DESC
                     LIMIT 1
                    """,
                    (full_oid,),
                ).fetchone()
                assert heavy_details == ("2", "2")
            first.rollback()

        by_oid = {row[0]: row[1:] for row in rows}
        assert by_oid[full_oid][0:3] == ("full", False, "deferred")
        assert "heavy maintenance slots busy" in by_oid[full_oid][3]
        assert by_oid[delta_oid][0:3] == ("delta", True, "ok")
    finally:
        rvbbit.execute(f"DROP TABLE IF EXISTS {delta_name} CASCADE")
        rvbbit.execute(f"DROP TABLE IF EXISTS {full_name} CASCADE")


def test_capacity_loser_does_not_lock_sweep_or_amplify_run_rows(rvbbit):
    suffix = uuid.uuid4().hex[:8]
    names = [f"heavy_bound_{i}_{suffix}" for i in range(10)]
    target_oids = []
    try:
        rvbbit.execute("SELECT rvbbit.migrate()")
        for name in names:
            table_oid = _make_scheduled_table(rvbbit, name, build_baseline=True)
            rvbbit.execute(f"TRUNCATE TABLE {name}")
            rvbbit.execute(
                f"INSERT INTO {name} "
                "SELECT value, repeat(md5(value::text), 2) "
                "FROM generate_series(1, 2000) AS value"
            )
            rvbbit.execute(f"ANALYZE {name}")
            target_oids.append(table_oid)

        with psycopg.connect(RVBBIT_DSN, autocommit=False) as first:
            with psycopg.connect(RVBBIT_DSN, autocommit=False) as second:
                assert first.execute(
                    "SELECT rvbbit._try_claim_accel_heavy_slot(2)"
                ).fetchone() == (1,)
                assert second.execute(
                    "SELECT rvbbit._try_claim_accel_heavy_slot(2)"
                ).fetchone() == (2,)

                # Keep the worker transaction open after its scan. None of the
                # heavy table claims should be retained when capacity was the
                # reason the action could not start.
                with psycopg.connect(RVBBIT_DSN, autocommit=False) as caller:
                    rows = caller.execute(
                        "SELECT table_oid::oid, status, reason "
                        "FROM rvbbit.accel_tick_worker(1, 1, false, 1)"
                    ).fetchall()
                    assert 1 <= len(rows) <= 8
                    assert all(row[1] == "deferred" for row in rows)

                    with psycopg.connect(RVBBIT_DSN, autocommit=False) as probe:
                        claims = [
                            probe.execute(
                                "SELECT pg_try_advisory_xact_lock(%s)",
                                (_table_lock_key(table_oid),),
                            ).fetchone()[0]
                            for table_oid in target_oids
                        ]
                        assert all(claims)
                        probe.rollback()
                    caller.rollback()

                before = rvbbit.execute(
                    "SELECT coalesce(max(id), 0) FROM rvbbit.accel_tick_runs"
                ).fetchone()[0]
                returned = rvbbit.execute(
                    "SELECT count(*) FROM rvbbit.accel_tick_worker(1, 1, false, 1)"
                ).fetchone()[0]
                logged = rvbbit.execute(
                    "SELECT count(*) FROM rvbbit.accel_tick_runs WHERE id > %s",
                    (before,),
                ).fetchone()[0]
                assert 1 <= returned <= 8
                assert logged == returned

                second.rollback()
            first.rollback()
    finally:
        for name in reversed(names):
            rvbbit.execute(f"DROP TABLE IF EXISTS {name} CASCADE")


def test_unified_variant_worker_uses_the_shared_pool_and_logs_its_slot(rvbbit):
    name = f"heavy_layout_{uuid.uuid4().hex[:8]}"
    try:
        rvbbit.execute("SELECT rvbbit.migrate()")
        table_oid = _make_vortex_candidate(rvbbit, name)

        with psycopg.connect(RVBBIT_DSN, autocommit=False) as first:
            with psycopg.connect(RVBBIT_DSN, autocommit=False) as second:
                assert first.execute(
                    "SELECT rvbbit._try_claim_accel_heavy_slot(2)"
                ).fetchone() == (1,)
                assert second.execute(
                    "SELECT rvbbit._try_claim_accel_heavy_slot(2)"
                ).fetchone() == (2,)

                assert rvbbit.execute(
                    "SELECT * FROM rvbbit.layout_tick_worker(1, 1, false)"
                ).fetchall() == []
                assert rvbbit.execute(
                    "SELECT count(*) FROM rvbbit.variant_build_queue WHERE table_oid=%s",
                    (table_oid,),
                ).fetchone() == (1,)

                second.rollback()
                built = rvbbit.execute(
                    """
                    SELECT status, details ->> 'heavy_slot',
                           details ->> 'heavy_slot_limit'
                      FROM rvbbit.layout_tick_worker(1, 1, false)
                     WHERE table_oid = %s
                    """,
                    (table_oid,),
                ).fetchone()
                assert built == ("ok", "2", "2")
            first.rollback()
    finally:
        rvbbit.execute(f"DROP TABLE IF EXISTS {name} CASCADE")


def test_legacy_variant_paths_are_also_attached_to_the_pool(rvbbit):
    rvbbit.execute("SELECT rvbbit.migrate()")
    definitions = rvbbit.execute(
        """
        SELECT target.signature,
               position(
                   '_try_claim_accel_heavy_slot'
                   IN pg_get_functiondef(target.signature::regprocedure)
               ) > 0 AS pooled
          FROM unnest(ARRAY[
              'rvbbit._accel_tick_batch(integer,boolean,integer)',
              'rvbbit.variant_tick(integer,boolean)',
              'rvbbit.workload_layout_tick_worker(integer,integer,boolean)',
              'rvbbit.layout_tick_worker(integer,integer,boolean)'
          ]) AS target(signature)
         ORDER BY target.signature
        """
    ).fetchall()
    assert definitions and all(pooled for _, pooled in definitions)

    freshness_definition = rvbbit.execute(
        "SELECT pg_get_functiondef("
        "'rvbbit._accel_tick_batch(integer,boolean,integer)'::regprocedure)"
    ).fetchone()[0]
    assert "capacity-eligible candidates only" in freshness_definition
    assert "Claim shared heavy capacity before the table claim" in freshness_definition
    assert "One worker invocation owns at most one table action" in freshness_definition
