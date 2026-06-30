#!/usr/bin/env bash
# Build a seed Rvbbit route profile from forced native/Duck/DataFusion/heap ClickBench runs.
#
# Optional env:
#   BENCH_LIMIT, BENCH_QUERIES, BENCH_REPEATS, BENCH_TIMEOUT, SKIP_LOAD
#   RVBBIT_ROUTE_PROFILE_OUT defaults to bench/clickbench/results/rvbbit_route_profile.clickbench_${BENCH_LIMIT}.json
#   RVBBIT_ROUTE_RESULTS_OUT optionally archives last_run.json before training

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

OUT_HOST="${RVBBIT_ROUTE_PROFILE_OUT:-bench/clickbench/results/rvbbit_route_profile.clickbench_${BENCH_LIMIT:-10000000}.json}"
OUT_CONTAINER="/bench/${OUT_HOST#bench/}"
RESULTS_HOST="${RVBBIT_ROUTE_RESULTS_OUT:-bench/clickbench/results/last_run.json}"
RESULTS_CONTAINER="/bench/${RESULTS_HOST#bench/}"

DEFAULT_SYSTEMS="rvbbit_native_forced,rvbbit_duck_forced,rvbbit_duck_vortex_forced,rvbbit_datafusion_mem_forced,rvbbit_datafusion_forced,rvbbit_datafusion_vortex_forced,rvbbit_pg_heap_forced"
if [ "${RVBBIT_ROUTE_INCLUDE_HIVE:-1}" != "0" ]; then
    DEFAULT_SYSTEMS="rvbbit_native_forced,rvbbit_duck_forced,rvbbit_duck_hive_forced,rvbbit_duck_vortex_forced,rvbbit_datafusion_mem_forced,rvbbit_datafusion_forced,rvbbit_datafusion_hive_forced,rvbbit_datafusion_vortex_forced,rvbbit_pg_heap_forced"
fi

BENCH_SYSTEMS="${BENCH_SYSTEMS:-${DEFAULT_SYSTEMS}}" \
RVBBIT_COMPACT_KEEP_HEAP="${RVBBIT_COMPACT_KEEP_HEAP:-1}" \
RVBBIT_REFRESH_LAYOUT_VARIANTS_AFTER_LOAD="${RVBBIT_REFRESH_LAYOUT_VARIANTS_AFTER_LOAD:-${RVBBIT_ROUTE_INCLUDE_HIVE:-1}}" \
    ./bench/clickbench/run_offline.sh

if [ "${RESULTS_HOST}" != "bench/clickbench/results/last_run.json" ]; then
    mkdir -p "$(dirname "${RESULTS_HOST}")"
    cp bench/clickbench/results/last_run.json "${RESULTS_HOST}"
fi

docker compose -f docker/docker-compose.yml -f docker/docker-compose.competitors.yml \
    exec -T bench python /bench/rvbbit_route_train.py \
    --suite clickbench \
    --results "${RESULTS_CONTAINER}" \
    --scale-rows "${BENCH_LIMIT:-10000000}" \
    --output "${OUT_CONTAINER}"

echo "route results: ${RESULTS_HOST}"
echo "route profile: ${OUT_HOST}"
