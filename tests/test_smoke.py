"""Sanity checks: extension loaded, catalog populated."""


def test_extension_loaded(rvbbit):
    row = rvbbit.execute("SELECT rvbbit.rvbbit_version()").fetchone()
    assert row is not None
    # Loose semver match so test doesn't break on every version bump.
    parts = row[0].split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts), row[0]


def test_rvbbit_schema_present(rvbbit):
    row = rvbbit.execute(
        "SELECT count(*) FROM pg_namespace WHERE nspname = 'rvbbit'"
    ).fetchone()
    assert row[0] == 1


def test_catalog_tables_present(rvbbit):
    expected = {
        "tables",
        "row_groups",
        "delete_log",
        "shreds",
        "operators",
        "receipts",
        "capability_catalog",
    }
    rows = rvbbit.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'rvbbit'"
    ).fetchall()
    present = {r[0] for r in rows}
    missing = expected - present
    assert not missing, f"missing catalog tables: {missing}"


def test_capability_catalog_seeded(rvbbit):
    row = rvbbit.execute(
        """
        SELECT
          count(*) FILTER (WHERE active) AS active_entries,
          count(*) FILTER (WHERE active AND kind = 'runtime_sidecar') AS runtime_entries,
          count(*) FILTER (WHERE active AND id = 'runtimes/python-runtime') AS python_runtime_entries,
          count(*) FILTER (
            WHERE active
              AND id = 'smoke/warren-echo'
              AND catalog_entry->'acceptance_tests' ? 'echo_operator_sample_table'
          ) AS acceptance_entries
        FROM rvbbit.capability_catalog
        """
    ).fetchone()
    assert row[0] >= 1
    assert row[1] >= 1
    assert row[2] == 1
    assert row[3] == 1


def test_access_method_registered(rvbbit):
    row = rvbbit.execute(
        "SELECT count(*) FROM pg_am WHERE amname = 'rvbbit'"
    ).fetchone()
    assert row[0] == 1
