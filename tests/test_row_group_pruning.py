"""Row-group min/max pruning for custom scans."""


def _make_prune_table(rvbbit, temp_table):
    rvbbit.execute(f"CREATE TABLE {temp_table} (id int, label text) USING rvbbit")
    rvbbit.execute(
        f"INSERT INTO {temp_table} "
        f"SELECT g, 'v' || g FROM generate_series(1, 10) g"
    )
    rvbbit.execute(f"SELECT rvbbit.compact('{temp_table}'::regclass)")
    return temp_table


def _plan_text(rvbbit, sql):
    rows = rvbbit.execute(f"EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF) {sql}").fetchall()
    return "\n".join(r[0] for r in rows)


def test_out_of_range_equality_prunes_row_group(rvbbit, temp_table):
    t = _make_prune_table(rvbbit, temp_table)

    # SELECT label (not count) so the aggregate rewriter doesn't collapse
    # this into a metadata-only Result before the custom scan runs.
    plan = _plan_text(rvbbit, f"SELECT label FROM {t} WHERE id = 999")
    assert "Custom Scan (RvbbitParquetScan)" in plan
    assert "Pruned Row Groups: 1" in plan

    row = rvbbit.execute(f"SELECT count(*) FROM {t} WHERE id = 999").fetchone()
    assert row == (0,)


def test_in_range_equality_keeps_row_group(rvbbit, temp_table):
    t = _make_prune_table(rvbbit, temp_table)

    plan = _plan_text(rvbbit, f"SELECT label FROM {t} WHERE id = 5")
    assert "Custom Scan (RvbbitParquetScan)" in plan
    assert "Pruned Row Groups: 0" in plan

    row = rvbbit.execute(f"SELECT count(*) FROM {t} WHERE id = 5").fetchone()
    assert row == (1,)
