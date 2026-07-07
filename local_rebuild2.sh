
export RVBBIT_DIRECT_ACCEL_LOAD=1
export BENCH_REPEATS=3
export BENCH_SYSTEMS=pg_baseline,citus,hydra,alloydb,rvbbit,clickhouse
#export BENCH_SYSTEMS=alloydb,rvbbit,clickhouse
#export BENCH_SYSTEMS=rvbbit
#export BENCH_SYSTEMS=rvbbit_datafusion_forced,rvbbit_native_forced,rvbbit_duck_vortex_forced,rvbbit_duck_forced,rvbbit_gpu_gqe_forced,pg_baseline,citus,hydra,alloydb,clickhouse
#export BENCH_SYSTEMS=rvbbit_datafusion_forced,rvbbit_native_forced,rvbbit_datafusion_vortex_forced,rvbbit_duck_vortex_forced,rvbbit_duck_forced,rvbbit_gpu_gqe_forced
#export BENCH_SYSTEMS=rvbbit_datafusion_forced,rvbbit_native_forced,rvbbit_duck_vortex_forced,rvbbit_duck_forced,rvbbit_gpu_gqe_forced,rvbbit
#export BENCH_SYSTEMS=rvbbit_datafusion_forced,rvbbit_native_forced,rvbbit_duck_vortex_forced,rvbbit_gpu_gqe_forced,rvbbit
export RVBBIT_DIRECT_ACCEL_METADATA_PROFILE=minimal
export RVBBIT_REFRESH_LAYOUT_VARIANTS_AFTER_LOAD=async
export RVBBIT_DIRECT_ACCEL_CHUNK_ROWS=2000000
export RVBBIT_COMPACT_SCAN_CHUNK_ROWS=2000000
export RVBBIT_COMPACT_WRITER_THREADS=8

export RVBBIT_GQE_SHM_SIZE=12gb
export NVSHMEM_SYMMETRIC_SIZE=9G
export GQE_MAX_QUERY_MEMORY=9663676416
export RVBBIT_GQE_FLIGHT_FALLBACK=0
#export BENCH_QUERIES=Q23
unset BENCH_QUERIES

export RVBBIT_ROUTE_DATAFUSION_VORTEX_ALLOW_TEMPORAL=1
export RVBBIT_ROUTE_ML_ENABLED=1

export TEST_NAME=gpu_bench_test18

#export SKIP_LOAD=1
unset SKIP_LOAD

#export BENCH_SYSTEMS=rvbbit

## --rebuild --reset-rvbbit-extension

export BENCH_LIMIT=1000000
./bench/clickbench/run_offline.sh --test-name $TEST_NAME --rebuild --reset-rvbbit-extension

export TPCH_SCALE=0.5
./bench/tpch/run_offline.sh --test-name $TEST_NAME

export TPCDS_SCALE=0.5
./bench/tpcds/run_offline.sh --test-name $TEST_NAME
