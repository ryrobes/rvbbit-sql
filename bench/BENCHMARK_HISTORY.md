# Benchmark History

The offline benchmark scripts persist completed runs into the rvbbit benchmark
database by default. This is benchmark-owned state in the `bench_history`
schema, not extension-owned `rvbbit` state, so benchmark table reloads and
normal `ALTER EXTENSION pg_rvbbit UPDATE` runs do not remove it.

Set `BENCH_PERSIST_RESULTS=0` to skip recording a run.

Tables:

- `bench_history.runs`: one row per benchmark run, including run id, suite,
  scale, ClickBench row count, settings, report path, git state, summary JSON,
  and enriched raw `last_run.json`.
- `bench_history.query_results`: one row per query/system result with median
  milliseconds, status, and cold/warm/detail JSON.
- `bench_history.run_system_summary`: aggregate view for charting scale curves.
- `bench_history.tatp_system_summary`: TATP-specific view with TPS, p95, p99,
  transaction counts, and error counts.

Useful examples:

```sql
SELECT started_at, suite, scale, row_count, system,
       geomean_ms, suite_time_ms, p95_ms, max_ms, wins, failures
FROM bench_history.run_system_summary
ORDER BY started_at DESC, suite, system;
```

```sql
SELECT scale, row_count, system, suite_time_ms / 1000.0 AS suite_seconds
FROM bench_history.run_system_summary
WHERE suite = 'ClickBench'
ORDER BY row_count, system;
```

```sql
SELECT run_id, qid, system, median_ms
FROM bench_history.query_results
WHERE suite = 'TPC-DS' AND qid IN ('Q4', 'Q14', 'Q17')
ORDER BY started_at, qid, system;
```

```sql
SELECT test_name, started_at, system, tps, median_ms, p95_ms, p99_ms, errors
FROM bench_history.tatp_system_summary
ORDER BY started_at DESC, system;
```

Use `BENCH_RUN_ID=<name>` to force a stable run id. Use
`--test-name <name>` / `--name <name>` or `BENCH_TEST_NAME=<name>` to group a
batch of runs in charts.
