
export RVBBIT_DIRECT_ACCEL_LOAD=1 ;
export BENCH_LIMIT=60000 ;
export BENCH_SYSTEMS=pg_baseline,citus,hydra,alloydb,rvbbit,clickhouse ;
export RVBBIT_DIRECT_ACCEL_METADATA_PROFILE=minimal ;
export RVBBIT_REFRESH_LAYOUT_VARIANTS_AFTER_LOAD=async ;
export RVBBIT_DIRECT_ACCEL_CHUNK_ROWS=50000 ;
export RVBBIT_COMPACT_SCAN_CHUNK_ROWS=50000 ;
export RVBBIT_COMPACT_WRITER_THREADS=6 ;
#export SKIP_LOAD=1 ;
unset SKIP_LOAD ;
./bench/clickbench/run_offline.sh --test-name saturday_churn1 --rebuild --reset-rvbbit-extension
