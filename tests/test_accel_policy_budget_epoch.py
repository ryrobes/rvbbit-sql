import uuid


def _create_budgeted_table(conn, name: str, daily_budget: int = 2) -> int:
    conn.execute(f"CREATE TABLE {name} (id integer PRIMARY KEY) USING rvbbit")
    conn.execute(f"INSERT INTO {name} SELECT value FROM generate_series(1, 32) value")
    conn.execute(f"ANALYZE {name}")
    conn.execute(
        f"""
        SELECT rvbbit.set_accel_policy(
            '{name}'::regclass,
            strategy => 'scheduled',
            min_interval_secs => 0,
            daily_refresh_budget => {daily_budget},
            full_rebuild_drift_ratio => 2.0
        )
        """
    )
    return conn.execute(f"SELECT '{name}'::regclass::oid").fetchone()[0]


def _charge_budget(conn, table_oid: int, table_name: str, count: int) -> None:
    conn.execute(
        """
        INSERT INTO rvbbit.accel_tick_runs (
            table_oid, table_name, strategy, action, reason,
            executed, status, rows_written, ran_at
        )
        SELECT %s, %s, 'scheduled', 'full', 'test prior lifecycle',
               true, 'ok', 32, clock_timestamp()
          FROM generate_series(1, %s)
        """,
        (table_oid, table_name, count),
    )


def _planner_row(conn, table_oid: int):
    return conn.execute(
        """
        SELECT action, status, reason
          FROM rvbbit.accel_tick(NULL, true)
         WHERE table_oid = %s
        """,
        (table_oid,),
    ).fetchone()


def test_manual_reenrollment_starts_a_new_budget_epoch(rvbbit):
    rvbbit.execute("SELECT rvbbit.migrate()")
    name = f"budget_reenroll_{uuid.uuid4().hex[:8]}"
    try:
        table_oid = _create_budgeted_table(rvbbit, name)
        original_epoch = rvbbit.execute(
            "SELECT budget_epoch_at FROM rvbbit.accel_policy WHERE table_oid = %s",
            (table_oid,),
        ).fetchone()[0]
        _charge_budget(rvbbit, table_oid, name, 2)

        blocked = _planner_row(rvbbit, table_oid)
        assert blocked[0:2] == ("skip", "skip")
        assert blocked[2] == "daily budget 2 exhausted"

        rvbbit.execute(
            f"""
            SELECT rvbbit.set_accel_policy(
                '{name}'::regclass,
                strategy => 'manual',
                min_interval_secs => 0,
                daily_refresh_budget => 2
            )
            """
        )
        manual_epoch = rvbbit.execute(
            "SELECT budget_epoch_at FROM rvbbit.accel_policy WHERE table_oid = %s",
            (table_oid,),
        ).fetchone()[0]
        assert manual_epoch == original_epoch

        rvbbit.execute(
            f"""
            SELECT rvbbit.set_accel_policy(
                '{name}'::regclass,
                strategy => 'scheduled',
                min_interval_secs => 0,
                daily_refresh_budget => 2,
                full_rebuild_drift_ratio => 2.0
            )
            """
        )
        reenrolled_epoch = rvbbit.execute(
            "SELECT budget_epoch_at FROM rvbbit.accel_policy WHERE table_oid = %s",
            (table_oid,),
        ).fetchone()[0]
        assert reenrolled_epoch > original_epoch

        # The receipts remain present for audit, but no longer spend this
        # lifecycle's budget. Exercise the real worker so its post-claim budget
        # recheck is covered as well as the dry-run planner.
        assert rvbbit.execute(
            "SELECT count(*) FROM rvbbit.accel_tick_runs WHERE table_oid = %s",
            (table_oid,),
        ).fetchone() == (2,)
        planned = _planner_row(rvbbit, table_oid)
        assert planned[1] == "planned"

        executed = rvbbit.execute(
            """
            SELECT executed, status
              FROM rvbbit.accel_tick_worker(1, 1, false, 1)
             WHERE table_oid = %s
            """,
            (table_oid,),
        ).fetchone()
        assert executed == (True, "ok")
    finally:
        rvbbit.execute(f"DROP TABLE IF EXISTS {name} CASCADE")


def test_active_policy_edit_does_not_refill_budget(rvbbit):
    rvbbit.execute("SELECT rvbbit.migrate()")
    name = f"budget_edit_{uuid.uuid4().hex[:8]}"
    try:
        table_oid = _create_budgeted_table(rvbbit, name, daily_budget=1)
        original_epoch = rvbbit.execute(
            "SELECT budget_epoch_at FROM rvbbit.accel_policy WHERE table_oid = %s",
            (table_oid,),
        ).fetchone()[0]
        _charge_budget(rvbbit, table_oid, name, 1)
        assert _planner_row(rvbbit, table_oid)[2] == "daily budget 1 exhausted"

        # An ordinary threshold/note edit remains in the same automation
        # lifecycle and must not become a budget-reset escape hatch.
        rvbbit.execute(
            f"""
            SELECT rvbbit.set_accel_policy(
                '{name}'::regclass,
                strategy => 'scheduled',
                min_interval_secs => 30,
                daily_refresh_budget => 1,
                full_rebuild_drift_ratio => 0.75,
                note => 'edited without re-enrollment'
            )
            """
        )
        edited_epoch = rvbbit.execute(
            "SELECT budget_epoch_at FROM rvbbit.accel_policy WHERE table_oid = %s",
            (table_oid,),
        ).fetchone()[0]
        assert edited_epoch == original_epoch
        assert _planner_row(rvbbit, table_oid)[2] == "daily budget 1 exhausted"
    finally:
        rvbbit.execute(f"DROP TABLE IF EXISTS {name} CASCADE")
