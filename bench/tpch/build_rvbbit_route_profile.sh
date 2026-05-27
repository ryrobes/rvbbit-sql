#!/usr/bin/env bash
# Build a seed Rvbbit route profile from forced native/Duck/DataFusion/heap TPC-H runs.
#
# Optional env:
#   TPCH_SCALE, BENCH_QUERIES, BENCH_REPEATS, BENCH_TIMEOUT, SKIP_LOAD
#   RVBBIT_ROUTE_PROFILE_OUT defaults to bench/tpch/results/rvbbit_route_profile.tpch_sf${TPCH_SCALE}.json
#   RVBBIT_ROUTE_RESULTS_OUT optionally archives last_run.json before training

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

SCALE="${TPCH_SCALE:-0.1}"
SCALE_LABEL="${SCALE//./_}"
OUT_HOST="${RVBBIT_ROUTE_PROFILE_OUT:-bench/tpch/results/rvbbit_route_profile.tpch_sf${SCALE_LABEL}.json}"
OUT_CONTAINER="/bench/${OUT_HOST#bench/}"
RESULTS_HOST="${RVBBIT_ROUTE_RESULTS_OUT:-bench/tpch/results/last_run.json}"
RESULTS_CONTAINER="/bench/${RESULTS_HOST#bench/}"

DEFAULT_SYSTEMS="rvbbit_native_forced,rvbbit_duck_forced,rvbbit_datafusion_mem_forced,rvbbit_datafusion_forced,rvbbit_pg_heap_forced"
if [ "${RVBBIT_ROUTE_INCLUDE_HIVE:-1}" != "0" ]; then
    DEFAULT_SYSTEMS="rvbbit_native_forced,rvbbit_duck_forced,rvbbit_duck_hive_forced,rvbbit_datafusion_mem_forced,rvbbit_datafusion_forced,rvbbit_datafusion_hive_forced,rvbbit_pg_heap_forced"
fi

BENCH_SYSTEMS="${BENCH_SYSTEMS:-${DEFAULT_SYSTEMS}}" \
RVBBIT_COMPACT_KEEP_HEAP="${RVBBIT_COMPACT_KEEP_HEAP:-1}" \
RVBBIT_REFRESH_LAYOUT_VARIANTS_AFTER_LOAD="${RVBBIT_REFRESH_LAYOUT_VARIANTS_AFTER_LOAD:-${RVBBIT_ROUTE_INCLUDE_HIVE:-1}}" \
    ./bench/tpch/run_offline.sh

if [ "${RESULTS_HOST}" != "bench/tpch/results/last_run.json" ]; then
    mkdir -p "$(dirname "${RESULTS_HOST}")"
    cp bench/tpch/results/last_run.json "${RESULTS_HOST}"
fi

docker compose -f docker/docker-compose.yml -f docker/docker-compose.competitors.yml \
    exec -T bench python /bench/rvbbit_route_train.py \
    --suite tpch \
    --results "${RESULTS_CONTAINER}" \
    --output "${OUT_CONTAINER}"

echo "route results: ${RESULTS_HOST}"
echo "route profile: ${OUT_HOST}"
