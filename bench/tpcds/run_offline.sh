#!/usr/bin/env bash
# Offline TPC-DS-derived runner.
#
# Usage:
#   ./bench/tpcds/run_offline.sh
#   TPCDS_SCALE=1 BENCH_SYSTEMS=rvbbit,duckdb,pg_baseline,alloydb ./bench/tpcds/run_offline.sh
#   SKIP_LOAD=1 BENCH_QUERIES=Q1,Q3,Q14 ./bench/tpcds/run_offline.sh
#   RVBBIT_RESET_EXTENSION=1 ./bench/tpcds/run_offline.sh
#   ./bench/tpcds/run_offline.sh --rebuild --reset-rvbbit-extension
#   ./bench/tpcds/run_offline.sh --test-name nightly-main

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

COMPOSE="docker compose -f docker/docker-compose.yml -f docker/docker-compose.competitors.yml"
SCALE="${TPCDS_SCALE:-0.1}"
SCALE_LABEL="${SCALE//./_}"
SYSTEMS="${BENCH_SYSTEMS:-rvbbit,duckdb,pg_baseline,citus,hydra,alloydb}"
RVBBIT_SELECTED=0
if [[ ",${SYSTEMS}," == *",rvbbit,"* ]] || [[ ",${SYSTEMS}," == *",rvbbit_native,"* ]] || [[ ",${SYSTEMS}," == *",rvbbit_native_forced,"* ]] || [[ ",${SYSTEMS}," == *",rvbbit_duck_hot,"* ]] || [[ ",${SYSTEMS}," == *",rvbbit_duck_auto,"* ]] || [[ ",${SYSTEMS}," == *",rvbbit_duck_forced,"* ]] || [[ ",${SYSTEMS}," == *",rvbbit_duck_hive_forced,"* ]] || [[ ",${SYSTEMS}," == *",rvbbit_duck_vortex_forced,"* ]] || [[ ",${SYSTEMS}," == *",rvbbit_datafusion_forced,"* ]] || [[ ",${SYSTEMS}," == *",rvbbit_datafusion_hive_forced,"* ]] || [[ ",${SYSTEMS}," == *",rvbbit_datafusion_vortex_forced,"* ]] || [[ ",${SYSTEMS}," == *",rvbbit_datafusion_mem_forced,"* ]] || [[ ",${SYSTEMS}," == *",rvbbit_pg_heap_forced,"* ]] || [[ ",${SYSTEMS}," == *",rvbbit_pg_heap,"* ]] || [[ ",${SYSTEMS}," == *",pg_heap,"* ]]; then
    RVBBIT_SELECTED=1
fi
HIVE_FORCED_SELECTED=0
if [[ ",${SYSTEMS}," == *",rvbbit_duck_hive_forced,"* ]] || [[ ",${SYSTEMS}," == *",rvbbit_datafusion_hive_forced,"* ]]; then
    HIVE_FORCED_SELECTED=1
fi
VORTEX_FORCED_SELECTED=0
if [[ ",${SYSTEMS}," == *",rvbbit_datafusion_vortex_forced,"* ]] || [[ ",${SYSTEMS}," == *",rvbbit_duck_vortex_forced,"* ]]; then
    VORTEX_FORCED_SELECTED=1
fi
HIVE_REFRESH_DEFAULT="off"
if [ "${RVBBIT_SELECTED}" = "1" ]; then
    HIVE_REFRESH_DEFAULT="sync"
fi
HIVE_REFRESH_DISPLAY="${RVBBIT_REFRESH_LAYOUT_VARIANTS_AFTER_LOAD:-${HIVE_REFRESH_DEFAULT}}"
VORTEX_LAYOUT_DISPLAY="${RVBBIT_COMPACT_VORTEX_LAYOUT:-off}"
if [ "${VORTEX_FORCED_SELECTED}" = "1" ] && [ -z "${RVBBIT_COMPACT_VORTEX_LAYOUT:-}" ]; then
    VORTEX_LAYOUT_DISPLAY="on"
fi
QUERIES_ENV=()
[ -n "${BENCH_QUERIES:-}" ] && QUERIES_ENV=(-e "BENCH_QUERIES=${BENCH_QUERIES}")
DUCK_HOT_ENV=()
[ -n "${RVBBIT_DUCK_HOT_DEBUG:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_DUCK_HOT_DEBUG=${RVBBIT_DUCK_HOT_DEBUG}")
[ -n "${RVBBIT_DUCK_HOT_VALIDATE:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_DUCK_HOT_VALIDATE=${RVBBIT_DUCK_HOT_VALIDATE}")
[ -n "${RVBBIT_DUCK_HOT_MODE:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_DUCK_HOT_MODE=${RVBBIT_DUCK_HOT_MODE}")
[ -n "${RVBBIT_ROUTE_PROFILE:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_ROUTE_PROFILE=${RVBBIT_ROUTE_PROFILE}")
[ -n "${RVBBIT_ROUTE_TRACE:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_ROUTE_TRACE=${RVBBIT_ROUTE_TRACE}")
[ -n "${RVBBIT_ROUTE_LOG:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_ROUTE_LOG=${RVBBIT_ROUTE_LOG}")
[ -n "${RVBBIT_ROUTE_PROFILE_MIN_CONFIDENCE:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_ROUTE_PROFILE_MIN_CONFIDENCE=${RVBBIT_ROUTE_PROFILE_MIN_CONFIDENCE}")
[ -n "${RVBBIT_ROUTE_HIVE_MIN_CONFIDENCE:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_ROUTE_HIVE_MIN_CONFIDENCE=${RVBBIT_ROUTE_HIVE_MIN_CONFIDENCE}")
[ -n "${RVBBIT_ROUTE_DUCK_VECTOR:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_ROUTE_DUCK_VECTOR=${RVBBIT_ROUTE_DUCK_VECTOR}")
[ -n "${RVBBIT_ROUTE_DUCK_HIVE:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_ROUTE_DUCK_HIVE=${RVBBIT_ROUTE_DUCK_HIVE}")
[ -n "${RVBBIT_ROUTE_DATAFUSION_MEM:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_ROUTE_DATAFUSION_MEM=${RVBBIT_ROUTE_DATAFUSION_MEM}")
[ -n "${RVBBIT_ROUTE_DATAFUSION_VECTOR:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_ROUTE_DATAFUSION_VECTOR=${RVBBIT_ROUTE_DATAFUSION_VECTOR}")
[ -n "${RVBBIT_ROUTE_DATAFUSION_HIVE:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_ROUTE_DATAFUSION_HIVE=${RVBBIT_ROUTE_DATAFUSION_HIVE}")
[ -n "${RVBBIT_ROUTE_DATAFUSION_VORTEX:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_ROUTE_DATAFUSION_VORTEX=${RVBBIT_ROUTE_DATAFUSION_VORTEX}")
[ -n "${RVBBIT_ROUTE_HIVE:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_ROUTE_HIVE=${RVBBIT_ROUTE_HIVE}")
[ -n "${RVBBIT_ROUTE_PG_ROWSTORE:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_ROUTE_PG_ROWSTORE=${RVBBIT_ROUTE_PG_ROWSTORE}")
[ -n "${RVBBIT_ROUTE_RVBBIT_NATIVE:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_ROUTE_RVBBIT_NATIVE=${RVBBIT_ROUTE_RVBBIT_NATIVE}")
[ -n "${RVBBIT_ROUTE_FORCE_CANDIDATE:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_ROUTE_FORCE_CANDIDATE=${RVBBIT_ROUTE_FORCE_CANDIDATE}")
[ -n "${RVBBIT_NATIVE_ROUTER:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_NATIVE_ROUTER=${RVBBIT_NATIVE_ROUTER}")
[ -n "${RVBBIT_ROUTE_OBSERVE:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_ROUTE_OBSERVE=${RVBBIT_ROUTE_OBSERVE}")
[ -n "${RVBBIT_ROUTE_EXPLORE_PCT:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_ROUTE_EXPLORE_PCT=${RVBBIT_ROUTE_EXPLORE_PCT}")
[ -n "${RVBBIT_HIVE_LAYOUT:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_HIVE_LAYOUT=${RVBBIT_HIVE_LAYOUT}")
[ -n "${RVBBIT_HOT_STORE_BUDGET_MB:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_HOT_STORE_BUDGET_MB=${RVBBIT_HOT_STORE_BUDGET_MB}")
[ -n "${RVBBIT_HOT_STORE_ROUTE_MAX_ROWS:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_HOT_STORE_ROUTE_MAX_ROWS=${RVBBIT_HOT_STORE_ROUTE_MAX_ROWS}")
[ -n "${RVBBIT_DF_INPROCESS:-}" ] && DUCK_HOT_ENV+=(-e "RVBBIT_DF_INPROCESS=${RVBBIT_DF_INPROCESS}")
[ -n "${BENCH_WALL_TIMEOUT:-}" ] && DUCK_HOT_ENV+=(-e "BENCH_WALL_TIMEOUT=${BENCH_WALL_TIMEOUT}")
[ -n "${BENCH_WALL_TIMEOUT_GRACE:-}" ] && DUCK_HOT_ENV+=(-e "BENCH_WALL_TIMEOUT_GRACE=${BENCH_WALL_TIMEOUT_GRACE}")
LOAD_ENV=()
[ -n "${RVBBIT_HOT_LOAD_AFTER_LOAD:-}" ] && LOAD_ENV+=(-e "RVBBIT_HOT_LOAD_AFTER_LOAD=${RVBBIT_HOT_LOAD_AFTER_LOAD}")
[ -n "${RVBBIT_HOT_STORE_BUDGET_MB:-}" ] && LOAD_ENV+=(-e "RVBBIT_HOT_STORE_BUDGET_MB=${RVBBIT_HOT_STORE_BUDGET_MB}")
[ -n "${RVBBIT_HOT_STORE_ROUTE_MAX_ROWS:-}" ] && LOAD_ENV+=(-e "RVBBIT_HOT_STORE_ROUTE_MAX_ROWS=${RVBBIT_HOT_STORE_ROUTE_MAX_ROWS}")
[ -n "${RVBBIT_COMPACT_VARIANTS_SYNC:-}" ] && LOAD_ENV+=(-e "RVBBIT_COMPACT_VARIANTS_SYNC=${RVBBIT_COMPACT_VARIANTS_SYNC}")
if [ "${HIVE_FORCED_SELECTED}" = "1" ]; then
    LOAD_ENV+=(-e "RVBBIT_COMPACT_HIVE_LAYOUT=${RVBBIT_COMPACT_HIVE_LAYOUT:-on}")
elif [ -n "${RVBBIT_COMPACT_HIVE_LAYOUT:-}" ]; then
    LOAD_ENV+=(-e "RVBBIT_COMPACT_HIVE_LAYOUT=${RVBBIT_COMPACT_HIVE_LAYOUT}")
fi
if [ "${VORTEX_FORCED_SELECTED}" = "1" ]; then
    LOAD_ENV+=(-e "RVBBIT_COMPACT_VORTEX_LAYOUT=${RVBBIT_COMPACT_VORTEX_LAYOUT:-on}")
elif [ -n "${RVBBIT_COMPACT_VORTEX_LAYOUT:-}" ]; then
    LOAD_ENV+=(-e "RVBBIT_COMPACT_VORTEX_LAYOUT=${RVBBIT_COMPACT_VORTEX_LAYOUT}")
fi
if [ "${RVBBIT_SELECTED}" = "1" ] || [ -n "${RVBBIT_REFRESH_LAYOUT_VARIANTS_AFTER_LOAD:-}" ]; then
    LOAD_ENV+=(-e "RVBBIT_REFRESH_LAYOUT_VARIANTS_AFTER_LOAD=${HIVE_REFRESH_DISPLAY}")
fi
[ -n "${RVBBIT_COMPACT_HIVE_KEYS:-}" ] && LOAD_ENV+=(-e "RVBBIT_COMPACT_HIVE_KEYS=${RVBBIT_COMPACT_HIVE_KEYS}")
[ -n "${RVBBIT_COMPACT_HIVE_VARIANTS:-}" ] && LOAD_ENV+=(-e "RVBBIT_COMPACT_HIVE_VARIANTS=${RVBBIT_COMPACT_HIVE_VARIANTS}")
[ -n "${RVBBIT_COMPACT_HIVE_MIN_DISTINCT:-}" ] && LOAD_ENV+=(-e "RVBBIT_COMPACT_HIVE_MIN_DISTINCT=${RVBBIT_COMPACT_HIVE_MIN_DISTINCT}")
[ -n "${RVBBIT_COMPACT_HIVE_MAX_DISTINCT:-}" ] && LOAD_ENV+=(-e "RVBBIT_COMPACT_HIVE_MAX_DISTINCT=${RVBBIT_COMPACT_HIVE_MAX_DISTINCT}")
REPEATS="${BENCH_REPEATS:-3}"
TIMEOUT_S="${BENCH_TIMEOUT:-300}"
RVBBIT_RESET_EXTENSION="${RVBBIT_RESET_EXTENSION:-${RESET_RVBBIT_EXTENSION:-}}"
RVBBIT_LOAD_ROUTE_PROFILE="${RVBBIT_LOAD_ROUTE_PROFILE:-}"
BENCH_REBUILD="${BENCH_REBUILD:-}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_ID="${BENCH_RUN_ID:-tpcds_sf${SCALE_LABEL}_${STAMP}}"
BENCH_TEST_NAME="${BENCH_TEST_NAME:-tpcds}"
BENCH_PERSIST_RESULTS="${BENCH_PERSIST_RESULTS:-1}"
REPORT_FILE="bench/tpcds/results/tpcds_sf${SCALE_LABEL}_${STAMP}.txt"
RESULTS_DIR="$(dirname "${REPORT_FILE}")"
HOST_UID="$(id -u)"
HOST_GID="$(id -g)"

say()  { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31mXX %s\033[0m\n' "$*" >&2; exit 1; }
env_on() {
    case "${1:-}" in
        1|true|TRUE|yes|YES|on|ON) return 0 ;;
        *) return 1 ;;
    esac
}
fix_results_ownership() {
    mkdir -p "${RESULTS_DIR}" 2>/dev/null || true
    ${COMPOSE} exec -T bench sh -c \
        "mkdir -p /bench/tpcds/results && chown -R ${HOST_UID}:${HOST_GID} /bench/tpcds/results" \
        >/dev/null 2>&1 || warn "could not chown bench/tpcds/results from the bench container"
}
record_benchmark_history() {
    env_on "${BENCH_PERSIST_RESULTS}" || return 0
    local git_commit git_dirty_arg
    git_commit="$(git rev-parse --short=12 HEAD 2>/dev/null || true)"
    if [ -n "$(git status --porcelain 2>/dev/null || true)" ]; then
        git_dirty_arg="--git-dirty"
    else
        git_dirty_arg="--no-git-dirty"
    fi
    say "recording benchmark history (${RUN_ID})"
    if ! ${COMPOSE} exec -T bench python /bench/record_benchmark_run.py \
        --results /bench/tpcds/results/last_run.json \
        --results-path bench/tpcds/results/last_run.json \
        --report-path "${REPORT_FILE}" \
        --run-id "${RUN_ID}" \
        --test-name "${BENCH_TEST_NAME}" \
        --suite TPC-DS \
        --scale "${SCALE}" \
        --started-at "${STAMP}" \
        --git-commit "${git_commit}" \
        "${git_dirty_arg}" \
        --setting "scale=${SCALE}" \
        --setting "systems=${SYSTEMS}" \
        --setting "repeats=${REPEATS}" \
        --setting "timeout_s=${TIMEOUT_S}" \
        --setting "queries=${BENCH_QUERIES:-}" \
        --setting "skip_load=${SKIP_LOAD:-0}" \
        --setting "rebuild=${BENCH_REBUILD:-0}" \
        --setting "rvbbit_reset_extension=${RVBBIT_RESET_EXTENSION:-0}" \
        --setting "hive_refresh=${HIVE_REFRESH_DISPLAY}" \
        --setting "vortex_layout=${VORTEX_LAYOUT_DISPLAY}" \
        --setting "df_inprocess=${RVBBIT_DF_INPROCESS:-on}" \
        --setting "hot_store_budget_mb=${RVBBIT_HOT_STORE_BUDGET_MB:-512}" \
        --setting "hot_store_route_max_rows=${RVBBIT_HOT_STORE_ROUTE_MAX_ROWS:-500000}"; then
        warn "benchmark completed, but history recording failed"
    fi
}
usage() {
    awk 'NR > 1 && /^#/ {sub(/^# ?/, ""); print; next} NR > 1 {exit}' "$0"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --reset-rvbbit-extension|--clear-rvbbit-system-data)
            RVBBIT_RESET_EXTENSION=1
            ;;
        --load-route-profile)
            RVBBIT_LOAD_ROUTE_PROFILE=1
            ;;
        --skip-load)
            export SKIP_LOAD=1
            ;;
        --rebuild)
            BENCH_REBUILD=1
            ;;
        --test-name|--name)
            [ "$#" -ge 2 ] || die "$1 requires a value"
            BENCH_TEST_NAME="$2"
            shift
            ;;
        --test-name=*|--name=*)
            BENCH_TEST_NAME="${1#*=}"
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown argument: $1"
            ;;
    esac
    shift
done

command -v docker >/dev/null || die "docker not found in PATH"
[ -f "docker/docker-compose.yml" ] || die "expected repo root"

say "configuration"
echo "   scale       : ${SCALE}"
echo "   systems     : ${SYSTEMS}"
echo "   repeats     : ${REPEATS}"
echo "   timeout/q   : ${TIMEOUT_S}s"
echo "   report file : ${REPORT_FILE}"
echo "   run id      : ${RUN_ID}"
echo "   test name   : ${BENCH_TEST_NAME}"
echo "   rvbbit reset: $(env_on "${RVBBIT_RESET_EXTENSION}" && echo destructive || echo preserve-system-data)"
echo "   route import: $(env_on "${RVBBIT_LOAD_ROUTE_PROFILE}" && echo yes || echo no)"
echo "   rebuild     : $(env_on "${BENCH_REBUILD}" && echo yes || echo no)"
echo "   persist     : $(env_on "${BENCH_PERSIST_RESULTS}" && echo yes || echo no)"
echo "   df_inprocess: ${RVBBIT_DF_INPROCESS:-on (default)}"
echo "   hive refresh: ${HIVE_REFRESH_DISPLAY}"
echo "   vortex      : ${VORTEX_LAYOUT_DISPLAY}"
echo "   hot store   : budget=${RVBBIT_HOT_STORE_BUDGET_MB:-512}MB route_max_rows=${RVBBIT_HOT_STORE_ROUTE_MAX_ROWS:-500000}"

if env_on "${BENCH_REBUILD}"; then
    say "rebuilding pg-rvbbit + bench images from current source"
    ${COMPOSE} --profile bench build pg-rvbbit bench
fi

say "starting competitor containers (profile=bench)"
${COMPOSE} --profile bench up -d
sleep 5
fix_results_ownership

if [ "${RVBBIT_SELECTED}" = "1" ]; then
    if env_on "${RVBBIT_RESET_EXTENSION}"; then
        say "resetting pg_rvbbit extension (DESTRUCTIVE: drops rvbbit system/catalog data)"
        ${COMPOSE} exec -T pg-rvbbit psql -U postgres -d bench -v ON_ERROR_STOP=1 <<'SQL'
DROP EXTENSION IF EXISTS pg_rvbbit CASCADE;
SET rvbbit.duck_backend = off;
DROP SCHEMA IF EXISTS rvbbit CASCADE;
CREATE SCHEMA rvbbit;
CREATE EXTENSION pg_rvbbit;
SQL
    else
        say "ensuring pg_rvbbit extension (preserves rvbbit system/catalog data)"
        ${COMPOSE} exec -T pg-rvbbit psql -U postgres -d bench -v ON_ERROR_STOP=1 <<'SQL'
CREATE EXTENSION IF NOT EXISTS pg_rvbbit;
ALTER EXTENSION pg_rvbbit UPDATE;
SQL
    fi

    if env_on "${RVBBIT_LOAD_ROUTE_PROFILE}" && [ -f "bench/rvbbit_route_profile.json" ]; then
        say "loading Rvbbit route profile"
        ${COMPOSE} exec -T bench python /bench/rvbbit_route_load_profile.py \
            --profile /bench/rvbbit_route_profile.json \
            --name bench-combined
    fi
fi

say "generating TPC-DS parquet"
${COMPOSE} exec -T -e "TPCDS_SCALE=${SCALE}" bench python -u /bench/tpcds/generate_data.py

if [ -z "${SKIP_LOAD:-}" ]; then
    say "loading TPC-DS sf=${SCALE} into [${SYSTEMS}]"
    ${COMPOSE} exec -T \
        -e "TPCDS_SCALE=${SCALE}" -e "BENCH_SYSTEMS=${SYSTEMS}" "${LOAD_ENV[@]}" \
        bench python -u /bench/tpcds/load_all.py
else
    say "skipping load (SKIP_LOAD set)"
fi

say "running queries"
${COMPOSE} exec -T \
    -e "TPCDS_SCALE=${SCALE}" -e "BENCH_SYSTEMS=${SYSTEMS}" \
    -e "BENCH_REPEATS=${REPEATS}" -e "BENCH_TIMEOUT=${TIMEOUT_S}" \
    "${QUERIES_ENV[@]}" "${DUCK_HOT_ENV[@]}" \
    bench python -u /bench/tpcds/run_queries.py

say "formatting report"
fix_results_ownership
mkdir -p "${RESULTS_DIR}"
${COMPOSE} exec -T -e NO_COLOR=1 bench \
    python /bench/tpcds/format_report.py \
    > "${REPORT_FILE}"

${COMPOSE} exec -T -e FORCE_COLOR=1 bench \
    python /bench/tpcds/format_report.py

record_benchmark_history

say "report saved to ${REPORT_FILE}"
echo "raw JSON at bench/tpcds/results/last_run.json"
