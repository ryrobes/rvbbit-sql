# TATP-Style Transactional Benchmark

This is a compact transactional suite inspired by TATP. TATP models a telecom home-location-register workload: https://tatpbenchmark.sourceforge.net/

It is meant to answer a different question than ClickBench or TPC-H: does the system preserve read/update/insert/delete semantics, and what latency/throughput does that path have?

It is not an audited TPC benchmark.

```bash
./bench/tatp/run_offline.sh
TATP_SUBSCRIBERS=100000 TATP_TXNS=50000 TATP_CLIENTS=4 ./bench/tatp/run_offline.sh
TATP_TABLE_AM=heap BENCH_SYSTEMS=rvbbit,pg_baseline ./bench/tatp/run_offline.sh
RVBBIT_RESET_EXTENSION=1 ./bench/tatp/run_offline.sh
./bench/tatp/run_offline.sh --reset-rvbbit-extension
./bench/tatp/run_offline.sh --rebuild --reset-rvbbit-extension
./bench/tatp/run_offline.sh --test-name nightly-main
```

Environment:

- `TATP_SUBSCRIBERS`: subscriber cardinality. Default `100000`.
- `TATP_TXNS`: transactions per system. Default `20000`.
- `TATP_CLIENTS`: concurrent client threads. Default `1`.
- `TATP_TABLE_AM`: `native` uses Rvbbit for Rvbbit and columnar AMs for Hydra/Citus; `heap` uses heap tables everywhere.
- `BENCH_SYSTEMS`: default `rvbbit,pg_baseline,citus,hydra,alloydb`.
- `SKIP_LOAD=1`: reuse existing tables.
- `RVBBIT_RESET_EXTENSION=1`: destructive Rvbbit extension reset. This wipes
  extension-owned system data such as router profiles/observations and KG
  tables. The default is to preserve system data and run `ALTER EXTENSION
  UPDATE`.
- `BENCH_PERSIST_RESULTS=0`: skip recording the completed run into
  `bench_history.runs` and `bench_history.query_results`.
- `BENCH_RUN_ID` and `--test-name <name>` / `--name <name>` or
  `BENCH_TEST_NAME`: override the persisted run id or group related runs. See
  `bench/BENCHMARK_HISTORY.md` for SQL examples.
- `--rebuild`: rebuild the `pg-rvbbit` and `bench` images before running.

When loading is enabled, the TATP benchmark tables are replaced for a clean
transactional run. Extension-owned Rvbbit system state is preserved unless
`RVBBIT_RESET_EXTENSION=1` is set.

The transaction mix includes subscriber point reads, access-info reads, call-forwarding joins, subscriber updates by key and phone number, call-forwarding inserts, and deletes.
