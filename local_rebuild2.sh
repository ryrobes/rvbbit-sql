
export RVBBIT_DIRECT_ACCEL_LOAD=1
export BENCH_LIMIT=1000000
export BENCH_REPEATS=3
xport BENCH_SYSTEMS=pg_baseline,citus,hydra,alloydb,rvbbit,clickhouse
#export BENCH_SYSTEMS=alloydb,rvbbit,clickhouse
export BENCH_SYSTEMS=rvbbit
#export BENCH_SYSTEMS=rvbbit_datafusion_forced,rvbbit_native_forced,rvbbit_duck_vortex_forced,rvbbit_duck_forced,rvbbit_gpu_gqe_forced,pg_baseline,citus,hydra,alloydb,clickhouse
#export BENCH_SYSTEMS=rvbbit_datafusion_forced,rvbbit_native_forced,rvbbit_datafusion_vortex_forced,rvbbit_duck_vortex_forced,rvbbit_duck_forced,rvbbit_gpu_gqe_forced
#export BENCH_SYSTEMS=rvbbit_datafusion_forced,rvbbit_native_forced,rvbbit_duck_vortex_forced,rvbbit_duck_forced,rvbbit_gpu_gqe_forced,rvbbit
#export BENCH_SYSTEMS=rvbbit_datafusion_forced,rvbbit_native_forced,rvbbit_duck_vortex_forced,rvbbit_gpu_gqe_forced,rvbbit
export RVBBIT_DIRECT_ACCEL_METADATA_PROFILE=rich
export RVBBIT_REFRESH_LAYOUT_VARIANTS_AFTER_LOAD=async
export RVBBIT_DIRECT_ACCEL_CHUNK_ROWS=50000000
export RVBBIT_COMPACT_SCAN_CHUNK_ROWS=50000000
export RVBBIT_COMPACT_WRITER_THREADS=8

export RVBBIT_GQE_SHM_SIZE=12gb
export NVSHMEM_SYMMETRIC_SIZE=9G
export GQE_MAX_QUERY_MEMORY=9663676416
export RVBBIT_GQE_FLIGHT_FALLBACK=0
#export BENCH_QUERIES=Q23
unset BENCH_QUERIES

export RVBBIT_ROUTE_DATAFUSION_VORTEX_ALLOW_TEMPORAL=1
 export RVBBIT_ROUTE_ML_ENABLED=1

#export SKIP_LOAD=1
unset SKIP_LOAD
./bench/clickbench/run_offline.sh --test-name gpu_bench_test4 --rebuild --reset-rvbbit-extension

## --rebuild --reset-rvbbit-extension
# 5m - 600mb, 2 files - 23m - wins 8
# 1m
#
export TPCH_SCALE=0.33
./bench/tpch/run_offline.sh --test-name gpu_bench_test4
